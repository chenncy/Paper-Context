"""记忆话术分层路由单测。"""
from app.memory_retrieval_router import (
    MemoryRetrievalRoute,
    classify_memory_retrieval_route,
)


def test_session_local_route():
    assert (
        classify_memory_retrieval_route("刚才你说的 RAG 是什么意思")
        == MemoryRetrievalRoute.SESSION_LOCAL
    )


def test_global_profile_route():
    assert (
        classify_memory_retrieval_route("你记得我偏好用什么向量库吗")
        == MemoryRetrievalRoute.GLOBAL_PROFILE
    )


def test_global_over_session_when_both_cues():
    assert (
        classify_memory_retrieval_route("你记得我刚才说的偏好吗")
        == MemoryRetrievalRoute.GLOBAL_PROFILE
    )


def test_temporal_episodic_route():
    assert (
        classify_memory_retrieval_route("昨天我们讨论的那个 bug 怎么样了")
        == MemoryRetrievalRoute.TEMPORAL_EPISODIC
    )


def test_balanced_default():
    assert classify_memory_retrieval_route("介绍一下 HyDE") == MemoryRetrievalRoute.BALANCED
