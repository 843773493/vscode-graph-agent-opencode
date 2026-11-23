"""Goal 应用的一次工作；排队、并发、恢复及关闭由通用 worker 持有。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from app.abstractions.internal_message import PreparedInternalMessage
from app.schemas.internal_v2.goal import GoalStatus, SessionGoalStateDTO
from app.schemas.internal_v2.job import JobDTO
from app.schemas.internal_v2.task_usage import TaskDispatchDTO
from app.services.business.persistent_tasks.goal import (
    GoalProgressSample,
    budget_limited_message,
    continuation_message,
    objective_updated_message,
)
from app.services.orchestration.persistent_tasks.usage import (
    bind_execution,
    execution_sample,
)

if TYPE_CHECKING:
    from app.services.orchestration.persistent_tasks.runtime import (
        PersistentTaskRuntime,
    )


GoalWorkKind = Literal["idle", "objective", "agent_end", "failed", "cancelled"]


@dataclass(frozen=True)
class GoalWork:
    session_id: str
    kind: GoalWorkKind = "idle"
    job: JobDTO | None = None
    goal: SessionGoalStateDTO | None = None
    request_id: str | None = None
    gateway_id: str | None = None
    observed_at: datetime | None = None
    recovering: bool = False
    owner_id: str | None = None


async def _submit(runtime: PersistentTaskRuntime, goal: SessionGoalStateDTO,
                  message: PreparedInternalMessage, identity: str) -> None:
    key = f"goal:{goal.goal_id}:{identity}:{message.metadata['structured_prompt_kind']}"
    state = runtime.store.usage_state(goal.goal_id)
    dispatch = state.dispatch
    if dispatch is None or dispatch.idempotency_key != key:
        dispatch = TaskDispatchDTO(trigger_id=identity, idempotency_key=key, source_id=f"goal:{goal.goal_id}:state",
                                   revision=goal.revision, control_revision=goal.control_revision, message=message)
        runtime.store.record_dispatch(goal.goal_id, dispatch, runtime.clock())
    wakeup = runtime._dispatcher.submitted(goal.session_id, goal.thread_id, idempotency_key=key)
    if wakeup is None:
        if dispatch.message is None:
            raise RuntimeError("任务已记录投递，但原准入回执缺失")
        await runtime._orchestrator.submit_runtime_event(
            goal.session_id, dispatch.message, thread_id=goal.thread_id, source_id=dispatch.source_id,
            idempotency_key=key, delivery_policy="after_turn", background=True,
        )
        wakeup = runtime._dispatcher.submitted(goal.session_id, goal.thread_id, idempotency_key=key)
        if wakeup is None:
            raise RuntimeError("任务执行接受后缺少 durable wakeup")
    bind_execution(runtime, goal.goal_id, session_id=goal.session_id, thread_id=goal.thread_id,
                   job_id=wakeup.job_id, control_revision=dispatch.control_revision,
                   role="notice" if message.metadata.get("goal_budget_limited") else "work")
    runtime.store.record_dispatch(goal.goal_id, dispatch.model_copy(update={"job_id": wakeup.job_id, "message": None}), runtime.clock())


def _metadata(goal: SessionGoalStateDTO) -> dict[str, object]:
    return {"goal_continuation": True, "goal_id": goal.goal_id, "goal_objective": goal.objective,
            "goal_control_revision": goal.control_revision}


async def _finish_execution(runtime: PersistentTaskRuntime, session_id: str, *, goal_id: str | None,
                            job_id: str, outcome: str, tokens: int, elapsed_seconds: int,
                            control_revision: int | None = None) -> None:
    goals = runtime.goals
    goal = await goals.get(session_id)
    if goal is None or goal_id != goal.goal_id:
        return
    entry = goal.accounted_jobs.get(job_id)
    if entry is not None and entry.settled:
        await goals.account_job(session_id, job_id=job_id, tokens=tokens, elapsed_seconds=elapsed_seconds,
                                goal_id=goal_id, close_time=True, settled=True)
        return
    observed_revision = goal.control_revision if control_revision is None else control_revision
    controls_current = goal.control_revision == observed_revision
    goal = await goals.account_job(session_id, job_id=job_id, tokens=tokens, elapsed_seconds=elapsed_seconds,
                                   goal_id=goal_id, close_time=True)
    if goal is None:
        return

    async def settle() -> None:
        # 结算包含终态动作；记账后、续跑前崩溃仍从原执行引用恢复。
        await goals.account_job(session_id, job_id=job_id, tokens=tokens, elapsed_seconds=elapsed_seconds,
                                goal_id=goal_id, close_time=True, settled=True)

    if not controls_current or (goal.control_revision != observed_revision
            and not (goal.status == GoalStatus.budget_limited and goal.control_revision == observed_revision + 1)):
        await settle()
        return
    if outcome not in {"completed", "completed_empty"}:
        await goals.stop_for_execution(session_id, goal_id=goal.goal_id,
                                       expected_control_revision=observed_revision,
                                       cancelled=outcome == "cancelled")
        await settle()
        return
    async with runtime.action_admission() as admitted:
        if not admitted:
            return
        if goal.status == GoalStatus.budget_limited:
            async def finish_budget(current: SessionGoalStateDTO) -> None:
                await _submit(runtime, current, budget_limited_message(current, metadata={**_metadata(current), "goal_budget_limited": True}), job_id)

            await goals.dispatch_if_current(session_id, job_id, finish_budget, goal_id=goal_id,
                expected_control_revision=goal.control_revision, status=GoalStatus.budget_limited)
            await settle()
            return

        async def dispatch(current: SessionGoalStateDTO) -> None:
            await _submit(runtime, current, continuation_message(current, metadata=_metadata(current)), job_id)

        await goals.dispatch_if_current(session_id, job_id, dispatch, goal_id=goal_id,
                                         expected_control_revision=observed_revision)
        await settle()


async def _recover_execution(runtime: PersistentTaskRuntime, goal: SessionGoalStateDTO) -> bool:
    """直接恢复冻结投递和执行引用，不沿 Goal 提示身份扫描整段历史。"""
    state = runtime.store.usage_state(goal.goal_id)
    dispatch = state.dispatch
    if dispatch is not None and dispatch.message is not None:
        accepted = runtime._dispatcher.submitted(goal.session_id, goal.thread_id, idempotency_key=dispatch.idempotency_key)
        if accepted is not None:
            bind_execution(runtime, goal.goal_id, session_id=goal.session_id, thread_id=goal.thread_id,
                job_id=accepted.job_id, control_revision=dispatch.control_revision,
                role="notice" if dispatch.message.metadata.get("goal_budget_limited") else "work")
            runtime.store.record_dispatch(goal.goal_id, dispatch.model_copy(update={"job_id": accepted.job_id, "message": None}), runtime.clock())
        elif dispatch.revision == goal.revision:
            async def resume(current: SessionGoalStateDTO) -> None:
                assert dispatch.message is not None
                await _submit(runtime, current, dispatch.message, dispatch.trigger_id)

            await runtime.goals.dispatch_if_current(goal.session_id, dispatch.trigger_id, resume,
                goal_id=goal.goal_id, revision=goal.revision, expected_control_revision=dispatch.control_revision,
                status=GoalStatus.budget_limited if dispatch.message.metadata.get("goal_budget_limited") else GoalStatus.active)
        state = runtime.store.usage_state(goal.goal_id)
    current_execution = False
    for job_id, entry in state.accounted_jobs.items():
        reference = entry.execution
        if reference is None or entry.settled:
            continue
        progress, elapsed = await execution_sample(runtime, job_id, reference)
        if progress.outcome != "unknown" and reference.role == "work":
            await _finish_execution(runtime, goal.session_id, goal_id=goal.goal_id, job_id=job_id,
                outcome=progress.outcome, tokens=progress.total_tokens, elapsed_seconds=elapsed,
                control_revision=reference.control_revision)
        else:
            await runtime.goals.account_job(goal.session_id, goal_id=goal.goal_id, job_id=job_id,
                tokens=progress.total_tokens, elapsed_seconds=elapsed, close_time=progress.outcome != "unknown",
                settled=progress.outcome != "unknown")
        if reference.control_revision == goal.control_revision and reference.role == "work":
            current_execution = True
    return current_execution


async def process_goal_work(runtime: PersistentTaskRuntime, work: GoalWork) -> None:
    goals, jobs = runtime.goals, runtime.jobs
    if work.kind in {"agent_end", "failed", "cancelled"}:
        assert work.job is not None
        job = work.job
        job_id = job.job_id
        owner = work.owner_id
        goal = await goals.get(work.session_id)
        if goal is None or owner != goal.goal_id or job.thread_id != goal.thread_id:
            return
        entry = runtime.store.usage_state(goal.goal_id).accounted_jobs.get(job_id)
        if entry is None or entry.execution is None:
            raise RuntimeError("Goal 终态工作缺少持久执行归属")
        reference = entry.execution
        progress, elapsed = await execution_sample(runtime, job_id, reference)
        if progress.outcome == "unknown":
            return
        if reference.role == "work":
            await _finish_execution(runtime, work.session_id, goal_id=owner, job_id=job_id,
                outcome=progress.outcome, tokens=progress.total_tokens, elapsed_seconds=elapsed,
                control_revision=reference.control_revision)
        else:
            await runtime.goals.account_job(work.session_id, goal_id=owner, job_id=job_id,
                tokens=progress.total_tokens, elapsed_seconds=elapsed, close_time=True, settled=True)
        return

    async with runtime.action_admission() as admitted:
        if not admitted:
            return
        goal = await goals.get(work.session_id)
        if goal is None:
            return
        if work.recovering and await _recover_execution(runtime, goal):
            return
        if goal.status != GoalStatus.active:
            return
        if work.goal is not None and (goal.goal_id, goal.revision) != (work.goal.goal_id, work.goal.revision):
            return
        if work.job is not None and work.job.thread_id != goal.thread_id:
            return
        current_jobs = await jobs.list(session_id=work.session_id)
        if work.kind == "idle" and any(job.thread_id == goal.thread_id and job.status.value in {"queued", "running", "accepted", "streaming"}
                                       for job in current_jobs):
            return
        objective = work.kind == "objective"
        identity = f"objective:{goal.revision}" if objective else f"idle:{goal.goal_id}:{goal.revision}"
        marker = f"objective:{goal.goal_id}:{goal.revision}" if objective else identity
        async def dispatch(current: SessionGoalStateDTO) -> None:
            message = (objective_updated_message(current, metadata={**_metadata(current), "goal_objective_updated": True})
                       if objective else continuation_message(current, metadata=_metadata(current)))
            await _submit(runtime, current, message, identity)

        await goals.dispatch_if_current(work.session_id, marker, dispatch,
                                                   goal_id=goal.goal_id, revision=goal.revision)


async def sample_goal_progress(runtime: PersistentTaskRuntime, session_id: str) -> tuple[GoalProgressSample, ...]:
    """控制请求前只采样；冻结由原 Goal owner 在 CAS 提交中完成。"""
    samples = []
    goal = await runtime.goals.get(session_id)
    if goal is None:
        return ()
    for job in await runtime.jobs.list(session_id=session_id):
        entry = goal.accounted_jobs.get(job.job_id)
        if entry is not None and entry.execution is not None and job.status.value in {"accepted", "queued", "running", "streaming"}:
            samples.append(GoalProgressSample(goal.goal_id, job.job_id,
                max(0, int((datetime.now(job.created_at.tzinfo) - job.created_at).total_seconds()))))
    return tuple(samples)
