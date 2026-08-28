from types import SimpleNamespace

from app.schemas.internal_v2.goal import GoalStatus, SessionGoalDTO
from app.services.infrastructure.session_goal_store import SessionGoalStore


class _Resolver:
    def __init__(self, path):
        self.path = path

    def resolve_session_node(self, session_id: str):
        assert session_id == "sess_goal"
        return self.path

    def refresh(self):
        return [SimpleNamespace(kind="session", path=self.path)]


def test_goal_store_atomic_round_trip(tmp_path):
    session_path = tmp_path / "sess_goal"
    session_path.mkdir()
    store = SessionGoalStore(_Resolver(session_path))
    goal = SessionGoalDTO(
        goal_id="goal_1",
        session_id="sess_goal",
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
