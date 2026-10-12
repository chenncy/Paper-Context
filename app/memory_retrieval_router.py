# app/memory_retrieval_router.py
"""
按用户话术粗分类记忆召回策略（规则路由，零延迟）。

- session_local：刚才 / 上文 / 本轮 → 优先当前 session 窗口与摘要，少拉全局与跨会话情节
- global_profile：记得我 / 偏好 → 优先全局画像，跳过情节与图谱
- temporal_episodic：昨天 / 上次我们 → 跨 session 情节 + 时间元数据，图谱可选
- balanced：默认分层顺序
"""
from __future__ import annotations

import re
from enum import Enum
from typing import List, Pattern


class MemoryRetrievalRoute(str, Enum):
    SESSION_LOCAL = "session_local"
    GLOBAL_PROFILE = "global_profile"
    TEMPORAL_EPISODIC = "temporal_episodic"
    BALANCED = "balanced"


def _compile(patterns: List[str]) -> List[Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_SESSION_PATTERNS = _compile(
    [
        r"刚才",
        r"刚刚",
        r"上文",
        r"上面",
        r"之前说的",
        r"你刚",
        r"本轮",
        r"这一轮",
        r"继续",
        r"接着",
        r"刚才那",
        r"刚刚那",
    ]
)

_GLOBAL_PATTERNS = _compile(
    [
        r"记得我",
        r"你还记得",
        r"我的偏好",
        r"我喜欢",
        r"我习惯",
        r"全局",
        r"一直",
        r"默认用",
        r"我通常",
        r"我一般是",
        r"你知道我",
    ]
)

_TEMPORAL_PATTERNS = _compile(
    [
        r"昨天",
        r"前天",
        r"上周",
        r"上个月",
        r"上次我们",
        r"之前我们",
        r"那天",
        r"早些时候",
        r"以前讨论",
        r"之前聊过",
    ]
)


def _matches_any(text: str, patterns: List[Pattern[str]]) -> bool:
    return any(p.search(text) for p in patterns)


def classify_memory_retrieval_route(query: str) -> MemoryRetrievalRoute:
    q = (query or "").strip()
    if not q:
        return MemoryRetrievalRoute.BALANCED
    # 全局偏好优先于「刚才」（避免「你记得我刚才说的」误判为 session_only）
    if _matches_any(q, _GLOBAL_PATTERNS):
        return MemoryRetrievalRoute.GLOBAL_PROFILE
    if _matches_any(q, _TEMPORAL_PATTERNS):
        return MemoryRetrievalRoute.TEMPORAL_EPISODIC
    if _matches_any(q, _SESSION_PATTERNS):
        return MemoryRetrievalRoute.SESSION_LOCAL
    return MemoryRetrievalRoute.BALANCED
