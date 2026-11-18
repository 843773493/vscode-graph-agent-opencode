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
        self.control_revision = 0
        self.sync_version = 0
        self.operations = {}

    def goal_read(self, session_id):
        return self.goal

    def goal_write(self, goal, *, expected_control_revision=None, operation=None, retiring_goal=None):
        if expected_control_revision is not None and expected_control_revision != self.control_revision:
            from app.abstractions.state_conflict import ClientStateConflictError
            raise ClientStateConflictError("Goal 控制版本冲突")
        self.goal = goal
        self.control_revision = goal.control_revision
        self.sync_version = goal.sync_version
        if operation is not None:
            self.operations[operation[0]] = operation[2]

    def goal_clear_versioned(self, session_id, expected_control_revision, operation=None, settled_goal=None):
        if expected_control_revision != self.control_revision:
            from app.abstractions.state_conflict import ClientStateConflictError
            raise ClientStateConflictError("Goal 控制版本冲突")
        existed = self.goal is not None
        self.goal = None
        if existed:
            self.control_revision += 1
            self.sync_version += 1
        result = (existed, self.control_revision, self.sync_version)
        if operation is not None:
            self.operations[operation[0]] = operation[2]
        return result

    def goal_control_revision(self, session_id):
        return self.control_revision

    def goal_sync_version(self, session_id):
        return self.sync_version

    def goal_operation(self, session_id, operation_id, request_hash=None):
        return self.operations.get(operation_id)

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

    assert await goal_service.dispatch_if_current("sess_1", "job_1", dispatch)
    assert [current.thread_id for current in continued] == [goal.thread_id]
    cleared = await goal_service.clear("sess_1", operation_id="clear-1", expected_control_revision=1)
    assert cleared.status == "applied" and cleared.goal is None and cleared.control_revision == 2
    assert cleared.session_id == "sess_1" and cleared.thread_id == goal.thread_id
    assert events[-1]["payload"] == {
        "session_id": "sess_1", "thread_id": goal.thread_id,
        "control_revision": 2, "sync_version": cleared.sync_version,
    }
    event_count = len(events)
    empty = await goal_service.clear("sess_1", operation_id="clear-2", expected_control_revision=2)
    assert empty.thread_id == goal.thread_id and empty.status == "already_missing"
    assert len(events) == event_count


@pytest.mark.asyncio
async def test_continuation_attempt_does_not_publish_a_marker_before_acceptance(goal_service, goal_store):
    original = await goal_service.set("sess_1", objective="未接受不能记为已续跑")
    attempts = []

    async def dispatch(current):
        assert goal_store.goal == original
        attempts.append(current.goal_id)
        if len(attempts) == 1:
            raise RuntimeError("continuation-not-accepted")

    with pytest.raises(RuntimeError, match="continuation-not-accepted"):
        await goal_service.dispatch_if_current("sess_1", "job_origin", dispatch)
    assert goal_store.goal == original
    assert await goal_service.dispatch_if_current("sess_1", "job_origin", dispatch)
    accepted = goal_store.goal
    assert accepted.goal_id == original.goal_id and accepted.last_continued_job_id == "job_origin"
    assert not await goal_service.dispatch_if_current("sess_1", "job_origin", dispatch)
    assert attempts == [original.goal_id, original.goal_id] and goal_store.goal == accepted


async def test_control_versions_advance_for_agent_and_budget_but_accounting_only_advances_sync(goal_service):
    created = await goal_service.create_for_agent("sess_1", "预算和控制水位", 20)
    counted = await goal_service.account_job("sess_1", job_id="job-1", tokens=10, elapsed_seconds=2)
    assert counted.control_revision == created.control_revision
    assert counted.sync_version > created.sync_version
    limited = await goal_service.account_job("sess_1", job_id="job-1", tokens=20, elapsed_seconds=3)
    assert limited.status == GoalStatus.budget_limited
    assert limited.control_revision == counted.control_revision + 1
    complete = await goal_service.update_for_agent("sess_1", GoalStatus.complete)
    assert complete.control_revision == limited.control_revision + 1
    replacement = await goal_service.create_for_agent("sess_1", "下一个目标", None)
    paused = await goal_service.update_for_agent("sess_1", GoalStatus.paused)
    assert paused.control_revision == replacement.control_revision + 1


async def test_late_accounting_and_continuation_cannot_mutate_replaced_goal(goal_service):
    old = await goal_service.set("sess_1", objective="旧目标")
    replacement = await goal_service.set("sess_1", objective="新目标", replace=True)
    assert await goal_service.account_job("sess_1", job_id="old-job", goal_id=old.goal_id,
        tokens=100, elapsed_seconds=100) is None
    dispatched = []

    async def dispatch(goal):
        dispatched.append(goal)

    assert not await goal_service.dispatch_if_current("sess_1", "old-job", dispatch, goal_id=old.goal_id)
    assert dispatched == []
    assert await goal_service.get("sess_1") == replacement
