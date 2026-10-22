from types import SimpleNamespace

import pytest

from app.schemas.internal_v2.goal import GoalStatus, SessionGoalDTO
from app.services.infrastructure.session_goal_store import SessionGoalStore


class _SessionCatalogPathResolverStub:
    def __init__(self, path):
        self.path = path

    def resolve_session_node(self, session_id: str):
        assert session_id == "sess_goal"
        return self.path

    def main_thread_id(self, session_id: str):
        assert session_id == "sess_goal"
        return "thr_019bf0c16c1c736783165d8b623efb7c"

    def list_nodes(self):
        return [SimpleNamespace(kind="session", node_id="sess_goal")]


@pytest.fixture
def goal_store_and_session_path(tmp_path):
    session_path = tmp_path / "sess_goal"
    session_path.mkdir()
    resolver = _SessionCatalogPathResolverStub(session_path)
    return SessionGoalStore(resolver), session_path


def test_goal_store_atomic_round_trip(goal_store_and_session_path):
    store, session_path = goal_store_and_session_path
    goal = SessionGoalDTO(
        goal_id="goal_1",
        session_id="sess_goal",
        thread_id="thr_019bf0c16c1c736783165d8b623efb7c",
        objective="完成目标",
        status=GoalStatus.active,
        created_at="2026-07-27T00:00:00Z",
        updated_at="2026-07-27T00:00:00Z",
    )

    store.write(goal)

    assert store.read("sess_goal") == goal
    assert store.list_existing() == [goal]
    assert not list(session_path.glob("*.tmp"))
    assert store.clear("sess_goal") is True
    assert store.read("sess_goal") is None


def test_goal_store_does_not_persist_derived_thread_identity(
    goal_store_and_session_path,
):
    """main thread 身份的唯一权威是 catalog，goal.json 不得落第二指针。"""
    store, session_path = goal_store_and_session_path
    goal = SessionGoalDTO(
        goal_id="goal_1",
        session_id="sess_goal",
        objective="完成目标",
        status=GoalStatus.active,
        created_at="2026-07-27T00:00:00Z",
        updated_at="2026-07-27T00:00:00Z",
        thread_id="thr_019bf0c16c1c736783165d8b623efb7c",
    )

    store.write(goal)

    raw = (session_path / "goal.json").read_text(encoding="utf-8")
    assert "thread_id" not in raw
    assert store.read("sess_goal").thread_id == goal.thread_id

    with pytest.raises(ValueError, match="权威 main Thread"):
        store.write(goal.model_copy(update={"thread_id": "thr_child"}))
    assert (session_path / "goal.json").read_text(encoding="utf-8") == raw
