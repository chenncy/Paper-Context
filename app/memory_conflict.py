"""记忆冲突裁决（从完整应用的固化技能模块抽出的纯机制层）。

将 new_facts 与已有全局画像比对，消解同 key 不同 value：
  - 无冲突：原样返回
  - category=preference：不直接覆盖；旧值写 key__version__{ts}，新值写 key__context__{session}
  - 其它冲突：打包给 LLM（CONFLICT_PROMPT）一次裁决，覆盖前为旧值留版本快照；
    LLM 可输出 {"key":..., "action":"retire"} → 走 supersede（时间感知淘汰，默认
    读取排除、可按 as_of 回看）

llm 以参数注入（不绑定任何具体 LLM 实现），外接服务/测试可传任意兼容对象。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List

from langchain_core.messages import HumanMessage
from sqlalchemy.engine import Engine

from app.config import GLOBAL_PROFILE_SESSION_ID
from app.memory import MemoryStore
from app.memory_contract import normalize_memory_type
from app.memory_metrics import record_conflict_stats

CONFLICT_PROMPT = """你是记忆策略助手。以下候选事实与已有全局画像存在键相同但值不同的情况，请决定最终写入的键值。
若用户是永久性改口，采用新值；若旧值仍可能适用于其它场景，可将 key 改为更具体的名称（如 preference_frontend_default）。
若判定某旧 key 的事实已整体失效（新事实以别的名称取代了它，或它已不再成立），输出 {"key": "旧key", "action": "retire"}，表示淘汰该旧事实且不写入新值。
只输出一个 JSON 数组，元素为 {"key","value","category"} 或 {"key","action":"retire"}，需覆盖所有待决议项。"""


def _parse_json_array(raw: str) -> List[dict]:
    """解析冲突消解等「JSON 数组」类 LLM 输出。"""
    t = (raw or "").strip()
    if not t:
        return []
    if "```" in t:
        a, b = t.find("["), t.rfind("]")
        if a != -1 and b > a:
            t = t[a : b + 1]
    try:
        data = json.loads(t)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def resolve_fact_conflicts(
    llm,
    engine: Engine,
    username: str,
    new_facts: List[dict],
    *,
    session_id: str = "",
) -> List[dict]:
    """
    将 new_global_facts 与已有全局画像（GLOBAL_PROFILE session）比对，消解同 key 不同 value。

    策略分层：
      - 无冲突：原样返回 normalized 列表
      - category=preference：不直接覆盖；旧值写入 key__version__{ts}，新值写入 key__context__{session}
      - 其它冲突：打包给 LLM（CONFLICT_PROMPT）一次裁决；覆盖前仍可为旧值留 version 快照
    """
    store = MemoryStore(engine, username, GLOBAL_PROFILE_SESSION_ID)
    old = store.load()
    normalized: List[dict] = []
    for f in new_facts:
        if not isinstance(f, dict):
            continue
        k = (f.get("key") or "").strip()
        if not k:
            continue
        v = str(f.get("value") or "").strip()
        cat = normalize_memory_type(f.get("category"))
        normalized.append({"key": k, "value": v, "category": cat})

    clean: List[dict] = []
    conflicts: List[dict] = []
    for f in normalized:
        k, v = f["key"], f["value"]
        if k in old and str(old[k]).strip() != v:
            conflicts.append(
                {"key": k, "previous": str(old[k]), "proposed": v, "category": f["category"]}
            )
        else:
            clean.append(f)

    if not conflicts:
        record_conflict_stats(total=0, resolved=0, checked=len(normalized))
        return normalized

    # 细粒度策略 1：偏好冲突优先「条件化」，避免直接覆盖全局画像。
    # 细粒度策略 2：覆盖前保留版本快照（key__version__{ts}），便于追溯与回滚。
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    resolved: List[dict] = list(clean)
    unresolved: List[dict] = []
    for c in conflicts:
        k = c["key"]
        prev = str(c["previous"]).strip()
        proposed = str(c["proposed"]).strip()
        cat = normalize_memory_type(c.get("category"))
        if cat == "preference":
            if prev:
                resolved.append(
                    {"key": f"{k}__version__{ts}", "value": prev, "category": cat}
                )
            suffix = (session_id or "global").replace(" ", "_")
            resolved.append(
                {
                    "key": f"{k}__context__{suffix}",
                    "value": proposed,
                    "category": cat,
                }
            )
        else:
            unresolved.append(c)

    if not unresolved:
        record_conflict_stats(total=len(conflicts), resolved=len(conflicts), checked=len(normalized))
        return resolved

    # 非 preference 冲突：单次 LLM 批量裁决
    payload = json.dumps(
        {"existing_profile": old, "conflicts": unresolved, "non_conflicting": resolved},
        ensure_ascii=False,
    )
    resp = llm.invoke([HumanMessage(content=CONFLICT_PROMPT + "\n\n" + payload)])
    raw = (resp.content if hasattr(resp, "content") else str(resp)) or ""
    merged = _parse_json_array(raw)
    llm_resolved: List[dict] = []
    retired_keys: List[str] = []
    for item in merged:
        if not isinstance(item, dict):
            continue
        k = (item.get("key") or "").strip()
        if not k:
            continue
        # 时间感知淘汰：LLM 裁决"旧 key 整体失效"→ 不写新值，
        # 显式标记 superseded（记录失效时刻，默认读取排除、可按 as_of 回看）
        if str(item.get("action") or "").strip().lower() == "retire":
            retired_keys.append(k)
            continue
        cat = normalize_memory_type(item.get("category"))
        llm_resolved.append(
            {
                "key": k,
                "value": str(item.get("value") or "").strip(),
                "category": cat,
            }
        )
    # 淘汰的旧事实写入取代链（失败不阻断主流程）
    for k in retired_keys:
        try:
            store.supersede(k)
        except Exception:
            pass

    if not llm_resolved:
        record_conflict_stats(
            total=len(conflicts),
            resolved=len(conflicts) - len(unresolved),
            checked=len(normalized),
        )
        return resolved
    # 对 LLM 给出的覆盖项追加版本化快照（若与 old 中同 key 值不同）
    by_key = {x["key"]: x for x in resolved}
    for x in llm_resolved:
        kk = x["key"]
        if kk in old and str(old[kk]).strip() != str(x.get("value") or "").strip():
            prev = str(old[kk]).strip()
            if prev:
                by_key[f"{kk}__version__{ts}"] = {
                    "key": f"{kk}__version__{ts}",
                    "value": prev,
                    "category": normalize_memory_type(x.get("category")),
                }
        by_key[x["key"]] = x
    record_conflict_stats(total=len(conflicts), resolved=len(conflicts), checked=len(normalized))
    return list(by_key.values())
