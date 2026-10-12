"""MemoryService 统一门面 + Candidate 审核测试。"""
import pytest

from app.config import GLOBAL_PROFILE_SESSION_ID
from app.memory import MemoryStore
from app.memory_management import confirm_memory_item, reject_memory_item
from app.memory_service import MemoryService, SessionMemoryService
from app.session_cache import invalidate_all_session_cache_for_user
from app.session_events import ensure_session_event_schema
from tests.memory_test_db import create_memory_test_engine


USER = "svc_user"
SID = "sess-svc-1"


@pytest.fixture
def service():
    engine = create_memory_test_engine()
    ensure_session_event_schema(engine)
    invalidate_all_session_cache_for_user(USER)
    yield MemoryService(engine)
    invalidate_all_session_cache_for_user(USER)


# ---------- 门面动词 ----------


def test_session_add_get_roundtrip(service):
    sess = service.session(USER, SID)
    sess.add("偏好语言", "Python", "preference")
    assert "Python" in sess.get("偏好语言")


def test_session_search_returns_memory_part(service):
    sess = service.session(USER, SID)
    sess.add("研究主题", "RAG 评测")
    result = sess.search("RAG")
    assert result["query"] == "RAG"
    assert isinstance(result["memory"], dict)
    # episodic/graph 在本地缺依赖时降级为空——不抛异常即为契约达标


def test_get_list_and_forget(service):
    store = MemoryStore(service.engine, USER, SID)
    store.save("k1", "v1")
    items = service.get(USER, scope="session", session_id=SID)
    assert any(i["memory_key"] == "k1" for i in items)

    target = next(i for i in items if i["memory_key"] == "k1")
    result = service.forget(USER, memory_id=int(target["id"]))
    assert result["status"] == "retracted"
    assert service.get(USER, scope="session", session_id=SID) == [] or all(
        i["memory_key"] != "k1" for i in service.get(USER, scope="session", session_id=SID)
    )


def test_forget_requires_id_or_scope(service):
    with pytest.raises(ValueError):
        service.forget(USER)


def test_update_creates_version_chain(service):
    store = MemoryStore(service.engine, USER, SID)
    store.save("city", "北京")
    items = service.get(USER, scope="session", session_id=SID)
    mid = int(next(i for i in items if i["memory_key"] == "city")["id"])

    updated = service.update(USER, mid, value="上海")
    assert updated is not None

    hist = service.history(USER, mid)
    assert hist is not None
    values = [str(h.get("memory_value")) for h in hist["chain"]]
    assert "北京" in values and "上海" in values  # 旧值保留在链上


def test_prepare_via_facade(service):
    store = MemoryStore(service.engine, USER, GLOBAL_PROFILE_SESSION_ID)
    store.save("方向", "Agent Runtime")
    pc = service.prepare(USER, SID, "方向是什么")
    assert pc.status == "ready"
    assert "方向" in pc.content


def test_handoff_accessor(service):
    from app.handoff import HandoffService, ensure_handoff_schema

    ensure_handoff_schema(service.engine)
    svc = service.handoff(USER)
    assert isinstance(svc, HandoffService)
    assert svc.list_pending() == []


# ---------- Candidate 审核（proposal 与 approved 分离） ----------
#
# 生产路径：固化管道把抽取的全局事实以 candidate 写入 GLOBAL_PROFILE
# （save_global_facts_from_list 默认 candidate）；candidate 不进入
# PreparedContext 召回，用户 confirm 晋升后才参与注入；reject 则撤回。
# 显式 get_one 读取 candidate 仍然允许（读命中可触发既有晋升机制），
# 这是设计内行为，不在本测试断言排除。


def _seed_candidate(service) -> int:
    store = MemoryStore(service.engine, USER, GLOBAL_PROFILE_SESSION_ID)
    store.save("疑似偏好", "喜欢深色主题", "preference", memory_status="candidate")
    items = service.get(USER, scope="global", include_candidates=True)
    return int(next(i for i in items if i["memory_key"] == "疑似偏好")["id"])


def test_candidate_not_in_prepared_context(service):
    """candidate 不参与 PreparedContext 召回（提示注入排除未审核提议）。"""
    mid = _seed_candidate(service)
    pc = service.prepare(USER, SID, "主题偏好")
    assert "深色" not in pc.content
    # include_candidates=False 的治理列表同样排除
    items = service.get(USER, scope="global", include_candidates=False)
    assert all(i["memory_key"] != "疑似偏好" for i in items)
    assert mid > 0


def test_confirm_promotes_candidate(service):
    mid = _seed_candidate(service)
    item = confirm_memory_item(service.engine, USER, mid)
    assert item is not None
    assert item["memory_status"] == "promoted"
    assert int(item.get("confirm_count") or 0) == 1

    # 晋升后进入 PreparedContext 召回
    invalidate_all_session_cache_for_user(USER)
    pc = service.prepare(USER, SID, "主题偏好")
    assert "深色" in pc.content

    # 重复 confirm 拒绝（非 candidate）
    assert confirm_memory_item(service.engine, USER, mid) is None


def test_reject_retracts_candidate(service):
    mid = _seed_candidate(service)
    assert reject_memory_item(service.engine, USER, mid) is True

    pc = service.prepare(USER, SID, "主题偏好")
    assert "深色" not in pc.content
    # 拒绝是终态：不能再 confirm
    assert confirm_memory_item(service.engine, USER, mid) is None


def test_review_scope_isolation(service):
    mid = _seed_candidate(service)
    assert confirm_memory_item(service.engine, "wrong_user", mid) is None
    assert reject_memory_item(service.engine, "wrong_user", mid) is False