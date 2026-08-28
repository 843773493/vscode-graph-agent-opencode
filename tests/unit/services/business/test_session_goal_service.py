import pytest

from app.schemas.internal_v2.goal import GoalStatus
from app.services.business.session_goal_service import SessionGoalService


class _SessionService:
    async def get(self, session_id: str):
        return {"session_id": session_id}


class _Store:
    def __init__(self):
        self.goal = None

    def read(self, session_id):
        return self.goal

    def write(self, goal):
        self.goal = goal

    def clear(self, session_id):
        existed = self.goal is not None
        self.goal = None
        return existed

    def list_existing(self):
        return [] if self.goal is None else [self.goal]


class _Bus:
    def __init__(self):
        self.events = []

    async def publish(self, **event):
        self.events.append(event)


@pytest.fixture
def goal_service():
    return SessionGoalService(
        store=_Store(), session_service=_SessionService(), job_event_bus=_Bus()
    )


@pytest.mark.asyncio
async def test_edit_preserves_goal_identity_and_usage(goal_service):
    created = await goal_service.set("sess_1", objective="旧目标", token_budget=100)
    await goal_service.account_job(
        "sess_1", job_id="job_1", tokens=30, elapsed_seconds=4
    )

    edited = await goal_service.set("sess_1", objective="新目标")

    assert edited.goal_id == created.goal_id
    assert edited.tokens_used == 30
    assert edited.time_used_seconds == 4
    assert edited.token_budget == 100


@pytest.mark.asyncio
async def test_budget_is_nullable_and_accounting_is_idempotent(goal_service):
    await goal_service.set("sess_1", objective="目标", token_budget=40)
    await goal_service.account_job(
        "sess_1", job_id="job_1", tokens=40, elapsed_seconds=2
    )
    duplicate = await goal_service.account_job(
        "sess_1", job_id="job_1", tokens=40, elapsed_seconds=2
    )

    assert duplicate.status == GoalStatus.budget_limited
    assert duplicate.tokens_used == 40
    cleared_budget = await goal_service.set("sess_1", token_budget=None)
    assert cleared_budget.token_budget is None


@pytest.mark.asyncio
async def test_agent_cannot_replace_unfinished_goal_or_set_user_status(goal_service):
    await goal_service.create_for_agent("sess_1", "目标", None)
    with pytest.raises(RuntimeError, match="已有未完成 Goal"):
        await goal_service.create_for_agent("sess_1", "另一个目标", None)
    with pytest.raises(ValueError, match="complete 或 blocked"):
        await goal_service.update_for_agent("sess_1", GoalStatus.paused)


@pytest.mark.asyncio
async def test_replace_resets_identity_and_usage(goal_service):
    old = await goal_service.set("sess_1", objective="旧目标", token_budget=100)
    await goal_service.account_job(
        "sess_1", job_id="job_1", tokens=30, elapsed_seconds=4
    )

    new = await goal_service.set("sess_1", objective="新目标", replace=True)

    assert new.goal_id != old.goal_id
    assert new.tokens_used == 0
    assert new.time_used_seconds == 0
    assert new.token_budget is None


@pytest.mark.asyncio
async def test_accounting_ledger_rejects_out_of_order_replay_and_freezes_time(
    goal_service,
):
    await goal_service.set("sess_1", objective="目标")
    await goal_service.account_job(
        "sess_1", job_id="job_1", tokens=10, elapsed_seconds=3
    )
    await goal_service.account_job(
        "sess_1", job_id="job_2", tokens=20, elapsed_seconds=4
    )
    await goal_service.account_job(
        "sess_1", job_id="job_1", tokens=10, elapsed_seconds=3
    )
    await goal_service.account_job(
        "sess_1", job_id="job_3", tokens=0, elapsed_seconds=5, close_time=True
    )
    final = await goal_service.account_job(
        "sess_1", job_id="job_3", tokens=7, elapsed_seconds=20
    )

    assert final.tokens_used == 37
    assert final.time_used_seconds == 12
