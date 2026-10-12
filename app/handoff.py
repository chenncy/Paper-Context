# app/handoff.py
"""
跨会话工作转交（Handoff，参考 PowerContext 的 Work continuity 设计）。

闭环四步：
    Work Contract（目标+边界） → Prepared Handoff（打包）
        → Acknowledgement（新会话接收） → Task Outcome（结果归档）

场景：用户周一在会话 A 研究"LoRA vs AdaLoRA 对比"，周三新开会话 B 说
"继续上次的研究"——系统把 A 的目标、已核实进展、卡点、下一步与证据引用
打包注入 B，无缝续接。

实现约定：
- 打包默认走规则装配（会话事件 + 摘要，零 LLM 成本、可离线测试）；
  传入 llm 时用 LLM 蒸馏为更紧凑的结构化包（schema 校验，失败回退规则装配）
- 转交包是长期记录（handoffs 表），注入通道复用 PreparedContext 语义：
  acknowledge 返回的内容带 evidence_refs（源会话事件 ID），可回溯
- 所有状态流转写入源/目标会话的 session_events（审计链完整）
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

STATUS_PREPARED = "prepared"
STATUS_COMMITTED = "committed"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

_VALID_TRANSITIONS = {
    STATUS_PREPARED: {STATUS_COMMITTED, STATUS_CANCELLED},
    STATUS_COMMITTED: {STATUS_ACKNOWLEDGED, STATUS_CANCELLED},
    STATUS_ACKNOWLEDGED: {STATUS_COMPLETED, STATUS_CANCELLED},
    STATUS_COMPLETED: set(),
    STATUS_CANCELLED: set(),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_handoff_schema(engine: Engine) -> None:
    """建 handoffs 表（幂等；正式迁移见 migrations/versions/006）。

    方言自适应：MySQL 用 AUTO_INCREMENT + INDEX 子句，SQLite（测试）用
    AUTOINCREMENT 且索引单独建。
    """
    is_sqlite = engine.dialect.name == "sqlite"
    if is_sqlite:
        ddl = """
            CREATE TABLE IF NOT EXISTS handoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username VARCHAR(50) NOT NULL,
                source_session_id VARCHAR(64) NOT NULL,
                target_session_id VARCHAR(64) NULL,
                objective TEXT NOT NULL,
                progress_json TEXT NOT NULL,
                blockers TEXT,
                next_steps TEXT,
                evidence_refs TEXT,
                status VARCHAR(16) NOT NULL DEFAULT 'committed',
                created_at VARCHAR(40) NOT NULL,
                acknowledged_at VARCHAR(40) NULL,
                completed_at VARCHAR(40) NULL,
                outcome_json TEXT NULL
            )
        """
    else:
        ddl = """
            CREATE TABLE IF NOT EXISTS handoffs (
                id INTEGER PRIMARY KEY AUTO_INCREMENT,
                username VARCHAR(50) NOT NULL,
                source_session_id VARCHAR(64) NOT NULL,
                target_session_id VARCHAR(64) NULL,
                objective TEXT NOT NULL,
                progress_json TEXT NOT NULL,
                blockers TEXT,
                next_steps TEXT,
                evidence_refs TEXT,
                status VARCHAR(16) NOT NULL DEFAULT 'committed',
                created_at VARCHAR(40) NOT NULL,
                acknowledged_at VARCHAR(40) NULL,
                completed_at VARCHAR(40) NULL,
                outcome_json TEXT NULL,
                INDEX idx_handoffs_user_status (username, status),
                INDEX idx_handoffs_source (username, source_session_id)
            )
        """
    with engine.begin() as conn:
        conn.execute(text(ddl))
        if is_sqlite:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_handoffs_user_status "
                    "ON handoffs(username, status)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_handoffs_source "
                    "ON handoffs(username, source_session_id)"
                )
            )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class HandoffService:
    """单用户视角的转交服务（username 在构造时绑定）。"""

    def __init__(self, engine: Engine, username: str):
        self.engine = engine
        self.username = username

    # ---------- 1. prepare：打包 ----------

    def prepare(
        self,
        source_session_id: str,
        db: Session,
        *,
        objective: str = "",
        next_steps: str = "",
        blockers: str = "",
        max_turns: int = 6,
        llm: Any = None,
    ) -> Dict[str, Any]:
        """
        把源会话的当前工作状态打包为转交草稿（不落库）。

        规则装配：objective 取用户传入或会话标题；progress 取最近
        max_turns 轮对话要点；evidence_refs 记录对应事件 ID。
        传入 llm 时优先 LLM 蒸馏，失败回退规则装配。
        """
        from app.session_events import load_session_events

        events = load_session_events(db, self.username, source_session_id, limit=200)
        turns = [
            e for e in events
            if e.get("event_type") in ("user.message", "assistant.message")
        ][-max_turns * 2:]

        progress: List[Dict[str, Any]] = []
        for e in turns:
            payload = e.get("payload") or {}
            content = str(payload.get("content") or "")
            progress.append(
                {
                    "role": payload.get("role") or e.get("event_type"),
                    "summary": content[:200],
                    "event_id": e.get("event_id"),
                }
            )

        if not objective:
            row = db.execute(
                text(
                    "SELECT title FROM agent_sessions "
                    "WHERE username = :u AND session_id = :s LIMIT 1"
                ),
                {"u": self.username, "s": source_session_id},
            ).first()
            objective = str(row[0]) if row else f"会话 {source_session_id} 的未完成工作"

        package: Dict[str, Any] = {
            "objective": objective,
            "progress": progress,
            "blockers": blockers,
            "next_steps": next_steps,
            "evidence_refs": [p["event_id"] for p in progress if p.get("event_id")],
        }

        if llm is not None:
            distilled = self._distill_with_llm(llm, package)
            if distilled:
                package = distilled

        package["source_session_id"] = source_session_id
        package["status"] = STATUS_PREPARED
        return package

    def _distill_with_llm(self, llm: Any, package: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """LLM 蒸馏为紧凑转交包；schema 校验失败返回 None（回退规则装配）。"""
        try:
            from langchain_core.messages import HumanMessage

            prompt = (
                "把以下工作会话状态蒸馏为转交包，输出 JSON："
                '{"objective": str, "progress": [{"summary": str}], '
                '"blockers": str, "next_steps": str}，不要多余文本。\n\n'
                + json.dumps(package, ensure_ascii=False)[:6000]
            )
            resp = llm.invoke([HumanMessage(content=prompt)])
            raw = (resp.content if hasattr(resp, "content") else str(resp)) or ""
            a, b = raw.find("{"), raw.rfind("}")
            if a < 0 or b <= a:
                return None
            data = json.loads(raw[a : b + 1])
            if not isinstance(data, dict) or not data.get("objective"):
                return None
            return {
                "objective": str(data["objective"])[:512],
                "progress": data.get("progress") or package["progress"],
                "blockers": str(data.get("blockers") or "")[:1000],
                "next_steps": str(data.get("next_steps") or "")[:1000],
                "evidence_refs": package["evidence_refs"],
            }
        except Exception as exc:
            logger.warning("handoff llm distill failed, fallback to rule-based: %s", exc)
            return None

    # ---------- 2. commit：落库 ----------

    def commit(self, package: Dict[str, Any]) -> int:
        """把转交包持久化为长期记录，返回 handoff_id。"""
        now = _utc_now()
        with self.engine.begin() as conn:
            r = conn.execute(
                text(
                    "INSERT INTO handoffs (username, source_session_id, objective, "
                    "progress_json, blockers, next_steps, evidence_refs, status, created_at) "
                    "VALUES (:u, :src, :obj, :prog, :blk, :nxt, :ev, :st, :now)"
                ),
                {
                    "u": self.username,
                    "src": str(package.get("source_session_id") or ""),
                    "obj": str(package.get("objective") or "")[:2000],
                    "prog": _json(package.get("progress") or []),
                    "blk": str(package.get("blockers") or "")[:1000],
                    "nxt": str(package.get("next_steps") or "")[:1000],
                    "ev": _json(package.get("evidence_refs") or []),
                    "st": STATUS_COMMITTED,
                    "now": now,
                },
            )
            return int(r.lastrowid or 0)

    # ---------- 查询 ----------

    def list_pending(self, *, limit: int = 20) -> List[dict]:
        """待认领的转交包（committed 且未被 acknowledge）。"""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM handoffs WHERE username = :u AND status = :st "
                    "ORDER BY created_at DESC LIMIT :lim"
                ),
                {"u": self.username, "st": STATUS_COMMITTED, "lim": max(1, min(limit, 100))},
            ).mappings()
            return [self._decode(dict(r)) for r in rows]

    def get(self, handoff_id: int) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM handoffs WHERE id = :id AND username = :u LIMIT 1"),
                {"id": int(handoff_id), "u": self.username},
            ).mappings().first()
        return self._decode(dict(row)) if row else None

    # ---------- 3. acknowledge：新会话接收 ----------

    def acknowledge(
        self, handoff_id: int, target_session_id: str, db: Session
    ) -> Optional[Dict[str, Any]]:
        """
        目标会话认领转交包：状态流转 + 双向审计事件，
        返回可注入新会话的转交内容（含 evidence_refs）。
        """
        item = self.get(handoff_id)
        if item is None or item["status"] != STATUS_COMMITTED:
            return None
        now = _utc_now()
        with self.engine.begin() as conn:
            r = conn.execute(
                text(
                    "UPDATE handoffs SET status = :new, target_session_id = :tgt, "
                    "acknowledged_at = :now WHERE id = :id AND username = :u "
                    "AND status = :old"
                ),
                {
                    "new": STATUS_ACKNOWLEDGED, "tgt": target_session_id, "now": now,
                    "id": handoff_id, "u": self.username, "old": STATUS_COMMITTED,
                },
            )
            if r.rowcount == 0:  # 并发认领竞争，只有一个成功
                return None

        from app.session_events import append_session_event

        for sid, etype in (
            (item["source_session_id"], "handoff.acknowledged"),
            (target_session_id, "handoff.received"),
        ):
            try:
                append_session_event(
                    db, username=self.username, session_id=sid,
                    event_type=etype,
                    payload={"handoff_id": handoff_id, "objective": item["objective"]},
                )
            except Exception as exc:
                logger.warning("handoff audit event failed (%s): %s", etype, exc)
        db.commit()

        item["status"] = STATUS_ACKNOWLEDGED
        item["target_session_id"] = target_session_id
        return item

    # ---------- 4. outcome：结果归档 ----------

    def outcome(self, handoff_id: int, result: Dict[str, Any], db: Session) -> bool:
        """任务完成归档：状态终态化 + 审计事件。"""
        item = self.get(handoff_id)
        if item is None or item["status"] != STATUS_ACKNOWLEDGED:
            return False
        now = _utc_now()
        with self.engine.begin() as conn:
            r = conn.execute(
                text(
                    "UPDATE handoffs SET status = :new, completed_at = :now, "
                    "outcome_json = :oc WHERE id = :id AND username = :u AND status = :old"
                ),
                {
                    "new": STATUS_COMPLETED, "now": now, "oc": _json(result),
                    "id": handoff_id, "u": self.username, "old": STATUS_ACKNOWLEDGED,
                },
            )
            if r.rowcount == 0:
                return False
        try:
            from app.session_events import append_session_event

            append_session_event(
                db, username=self.username,
                session_id=str(item.get("target_session_id") or ""),
                event_type="handoff.completed",
                payload={"handoff_id": handoff_id, "outcome": result},
            )
            db.commit()
        except Exception as exc:
            logger.warning("handoff outcome audit failed: %s", exc)
        return True

    def cancel(self, handoff_id: int) -> bool:
        """取消未完成的转交（任意非终态 → cancelled）。"""
        with self.engine.begin() as conn:
            r = conn.execute(
                text(
                    "UPDATE handoffs SET status = :new WHERE id = :id AND username = :u "
                    "AND status NOT IN (:done, :cancel)"
                ),
                {
                    "new": STATUS_CANCELLED, "id": handoff_id, "u": self.username,
                    "done": STATUS_COMPLETED, "cancel": STATUS_CANCELLED,
                },
            )
            return bool(r.rowcount)

    # ---------- 内部 ----------

    @staticmethod
    def _decode(row: dict) -> dict:
        for key, target in (("progress_json", "progress"), ("outcome_json", "outcome")):
            raw = row.pop(key, None)
            if raw:
                try:
                    row[target] = json.loads(raw)
                except Exception:
                    row[target] = raw
        for key in ("evidence_refs",):
            raw = row.get(key)
            if isinstance(raw, str):
                try:
                    row[key] = json.loads(raw)
                except Exception:
                    row[key] = []
        return row
