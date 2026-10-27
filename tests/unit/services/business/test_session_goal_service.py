import pytest

from app.schemas.internal_v2.goal import GoalStatus
from app.services.business.session_goal_service import SessionGoalService


class _GoalSessionServiceFake:
    async def get(self, session_id: str):
        return {"session_id": session_id}

    async def resolve_main_thread(self, session_id: str) -> str:
        return f"thr_{session_id}"


class _GoalStoreFake:
    def __init__(self):
        self.goal = None

    def goal_read(self, session_id):
        return self.goal

    def goal_write(self, goal):
        self.goal = goal

    def goal_clear(self, session_id):
        existed = self.goal is not None
        self.goal = None
        return existed

    def goal_list(self):
        return [] if self.goal is None else [self.goal]


class _GoalEventBusFake:
    def __init__(self):
        self.events = []

    async def publish(self, **event):
        self.events.append(event)


@pytest.fixture
def goal_store() -> _GoalStoreFake:
    return _GoalStoreFake()


@pytest.fixture
def goal_service(goal_store: _GoalStoreFake) -> SessionGoalService:
    return SessionGoalService(
        store=goal_store,
        session_service=_GoalSessionServiceFake(),
        job_event_bus=_GoalEventBusFake(),
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
async def test_agent_can_pause_goal_but_cannot_replace_or_resume_it(goal_service):
    await goal_service.create_for_agent("sess_1", "目标", None)
    with pytest.raises(RuntimeError, match="已有未完成 Goal"):
        await goal_service.create_for_agent("sess_1", "另一个目标", None)
    paused = await goal_service.update_for_agent("sess_1", GoalStatus.paused)
    assert paused.status == GoalStatus.paused
    with pytest.raises(ValueError, match="没有进行中的 Goal"):
        await goal_service.update_for_agent("sess_1", GoalStatus.paused)
    with pytest.raises(ValueError, match="paused、complete 或 blocked"):
        await goal_service.update_for_agent("sess_1", GoalStatus.active)


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


@pytest.mark.asyncio
async def test_budget_limited_is_derived_only_while_goal_is_active(goal_service):
    # budget_limited 只是 active Goal 的派生状态；一旦预算耗尽，Agent 仍必须
    # 能把真实完成的 Goal 收敛到 complete，用户也必须能显式改回 active。
    await goal_service.set("sess_1", objective="目标", token_budget=10)
    await goal_service.account_job(
        "sess_1", job_id="job_1", tokens=10, elapsed_seconds=1
    )
    assert (await goal_service.get("sess_1")).status == GoalStatus.budget_limited

    completed = await goal_service.update_for_agent("sess_1", GoalStatus.complete)
    assert completed.status == GoalStatus.complete

    reactivated = await goal_service.set("sess_1", status=GoalStatus.active)
    assert reactivated.status == GoalStatus.budget_limited

    paused = await goal_service.set("sess_1", status=GoalStatus.paused)
    assert paused.status == GoalStatus.paused


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [status for status in GoalStatus if status != GoalStatus.active])
async def test_accounting_does_not_override_non_active_status(goal_service, status):
    await goal_service.set("sess_1", objective="目标", token_budget=10)
    await goal_service.set("sess_1", status=status)

    accounted = await goal_service.account_job(
        "sess_1", job_id="job_1", tokens=10, elapsed_seconds=1
    )

    assert accounted.status == status
    assert accounted.tokens_used == 10


@pytest.mark.asyncio
async def test_goal_responses_carry_authoritative_main_thread(
    goal_service: SessionGoalService,
    goal_store: _GoalStoreFake,
) -> None:
    """Goal 只属于 main thread；响应必须暴露解析出的 thread 身份。"""
    created = await goal_service.set("sess_1", objective="目标")

    assert created.thread_id == "thr_sess_1"
    read = await goal_service.get("sess_1")
    assert read is not None
    assert read.thread_id == "thr_sess_1"
    # store 的内存 DTO 保留归属；任务应用状态排除该派生字段由 store 测试证明。
    assert goal_store.goal.thread_id == "thr_sess_1"


@pytest.mark.asyncio
async def test_accounting_continuation_and_clear_keep_main_owner(goal_service):
    goal = await goal_service.set("sess_1", objective="目标")
    accounted = await goal_service.account_job("sess_1", job_id="job_1", tokens=3, elapsed_seconds=2)
    assert accounted.thread_id == goal.thread_id == "thr_sess_1"
    events = goal_service._job_event_bus.events
    assert all(event["payload"]["thread_id"] == goal.thread_id for event in events)
    assert all(event["payload"]["goal"]["thread_id"] == goal.thread_id for event in events)
    continued = []

    async def dispatch(current):
        continued.append(current)

    assert await goal_service.dispatch_continuation_if_active("sess_1", "job_1", dispatch)
    assert [current.thread_id for current in continued] == [goal.thread_id]
    cleared = await goal_service.clear("sess_1")
    assert cleared.model_dump() == {"session_id": "sess_1", "thread_id": goal.thread_id, "cleared": True}
    assert events[-1]["payload"] == {"session_id": "sess_1", "thread_id": goal.thread_id}
    event_count = len(events)
    empty = await goal_service.clear("sess_1")
    assert empty.thread_id == goal.thread_id and not empty.cleared
    assert len(events) == event_count
