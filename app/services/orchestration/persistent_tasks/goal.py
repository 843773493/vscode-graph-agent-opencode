"""Goal 应用的一次工作；排队、并发、恢复及关闭由通用 worker 持有。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from app.abstractions.internal_message import PreparedInternalMessage
from app.core.shielded_cleanup import run_sync_shielded
from app.domain.itemized.runtime_wakeup import RuntimeExecutionWakeup
from app.schemas.internal_v2.goal import GoalStatus, SessionGoalStateDTO
from app.schemas.internal_v2.job import JobDTO
from app.services.business.persistent_tasks.goal import (
    GoalProgressSample,
    budget_limited_message,
    continuation_message,
    objective_updated_message,
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
    tokens: int = 0
    goal: SessionGoalStateDTO | None = None
    request_id: str | None = None
    gateway_id: str | None = None
    observed_at: datetime | None = None
    recovering: bool = False


async def _submit(runtime: PersistentTaskRuntime, goal: SessionGoalStateDTO,
                  message: PreparedInternalMessage, identity: str) -> None:
    await runtime._orchestrator.submit_runtime_event(
        goal.session_id, message, thread_id=goal.thread_id, source_id=f"goal:{goal.goal_id}:state",
        idempotency_key=f"goal:{goal.goal_id}:{identity}:{message.metadata['structured_prompt_kind']}",
        delivery_policy="after_turn",
    )


def _metadata(goal: SessionGoalStateDTO) -> dict[str, object]:
    return {"goal_continuation": True, "goal_id": goal.goal_id, "goal_objective": goal.objective}


async def _finish_execution(runtime: PersistentTaskRuntime, session_id: str, *, goal_id: str | None,
                            job_id: str, outcome: str, tokens: int, elapsed_seconds: int) -> None:
    goals = runtime.goals
    goal = await goals.get(session_id)
    if goal is None or goal_id != goal.goal_id:
        return
    if outcome not in {"completed", "completed_empty"}:
        await goals.stop_for_execution(session_id, goal_id=goal.goal_id,
                                       expected_control_revision=goal.control_revision,
                                       cancelled=outcome == "cancelled")
        return
    goal = await goals.account_job(session_id, job_id=job_id, tokens=tokens, elapsed_seconds=elapsed_seconds, goal_id=goal_id)
    if goal is None:
        return
    async with runtime.action_admission() as admitted:
        if not admitted:
            return
        if goal.status == GoalStatus.budget_limited:
            await _submit(runtime, goal, budget_limited_message(goal, metadata={**_metadata(goal), "goal_budget_limited": True}), job_id)
            return

        async def dispatch(current: SessionGoalStateDTO) -> None:
            await _submit(runtime, current, continuation_message(current, metadata=_metadata(current)), job_id)

        await goals.dispatch_continuation_if_active(session_id, job_id, dispatch, goal_id=goal_id)


def _recovery_wakeup(runtime: PersistentTaskRuntime, goal: SessionGoalStateDTO) -> RuntimeExecutionWakeup | None:
    """在 worker 沿原准入身份追踪当前 revision，不修改运行时状态。"""
    roots = (f"idle:{goal.goal_id}:{goal.revision}:goal_continuation",
             f"objective:{goal.revision}:goal_objective_updated")
    accepted = [wakeup for identity in roots if (wakeup := runtime._dispatcher.submitted(
        goal.session_id, goal.thread_id, idempotency_key=f"goal:{goal.goal_id}:{identity}",
    )) is not None]
    if not accepted:
        return None
    wakeup = max(accepted, key=lambda row: runtime._saver.execution_queue_record(
        row.session_id, row.thread_id, row.job_id,
    ).cursor.commit_id)
    while (following := runtime._dispatcher.submitted(
        goal.session_id, goal.thread_id,
        idempotency_key=f"goal:{goal.goal_id}:{wakeup.job_id}:goal_continuation",
    )) is not None:
        wakeup = following
    return wakeup


async def _recover_execution(runtime: PersistentTaskRuntime, goal: SessionGoalStateDTO) -> bool:
    """恢复丢失的终态消费；历史读取期间允许其他任务与用户操作继续。"""
    wakeup = await run_sync_shielded(_recovery_wakeup, runtime, goal)
    current = await runtime.goals.get(goal.session_id)
    if current is None or (current.goal_id, current.revision) != (goal.goal_id, goal.revision):
        return True
    if wakeup is None:
        return False
    # 末次状态与归属登记不让出事件循环，避免完成事件先于归属登记被消费。
    progress = runtime._saver.runtime_execution_progress(wakeup)
    if progress.outcome == "unknown":
        runtime._goal_job_owners[wakeup.job_id] = goal.goal_id
    else:
        await _finish_execution(
            runtime, goal.session_id, goal_id=goal.goal_id, job_id=wakeup.job_id,
            outcome=progress.outcome, tokens=progress.total_tokens,
            elapsed_seconds=max(0, int(datetime.fromisoformat(progress.updated_at).timestamp()
                                       - datetime.fromisoformat(wakeup.message_created_at).timestamp())),
        )
    return True


async def process_goal_work(runtime: PersistentTaskRuntime, work: GoalWork) -> None:
    goals, jobs = runtime.goals, runtime.jobs
    if work.kind in {"agent_end", "failed", "cancelled"}:
        assert work.job is not None
        job = work.job
        job_id = job.job_id
        owner = runtime._goal_job_owners.pop(job_id, None)
        goal = await goals.get(work.session_id)
        if goal is None or owner != goal.goal_id or job.thread_id != goal.thread_id:
            return
        assert work.observed_at is not None
        elapsed = (max(0, int((job.ended_at or work.observed_at).timestamp() - job.created_at.timestamp()))
                   if work.kind == "agent_end" else 0)
        await _finish_execution(runtime, work.session_id, goal_id=owner, job_id=job_id,
                                outcome="completed" if work.kind == "agent_end" else work.kind,
                                tokens=work.tokens, elapsed_seconds=elapsed)
        return

    async with runtime.action_admission() as admitted:
        if not admitted:
            return
        goal = await goals.get(work.session_id)
        if goal is None or goal.status != GoalStatus.active:
            return
        if work.goal is not None and (goal.goal_id, goal.revision) != (work.goal.goal_id, work.goal.revision):
            return
        if work.job is not None and work.job.thread_id != goal.thread_id:
            return
        if work.recovering and await _recover_execution(runtime, goal):
            return
        current_jobs = await jobs.list(session_id=work.session_id)
        if work.kind == "idle" and any(job.thread_id == goal.thread_id and job.status.value in {"queued", "running", "accepted", "streaming"}
                                       for job in current_jobs):
            return
        objective = work.kind == "objective"
        identity = f"objective:{goal.revision}" if objective else f"idle:{goal.goal_id}:{goal.revision}"
        marker = f"objective:{goal.goal_id}:{goal.revision}" if objective else identity
        owners = {job.job_id: goal.goal_id for job in current_jobs
                  if objective and job.thread_id == goal.thread_id and runtime._goal_job_owners.get(job.job_id) == goal.goal_id}
        for job_id in owners:
            runtime._goal_job_owners.pop(job_id)

        async def dispatch(current: SessionGoalStateDTO) -> None:
            message = (objective_updated_message(current, metadata={**_metadata(current), "goal_objective_updated": True})
                       if objective else continuation_message(current, metadata=_metadata(current)))
            await _submit(runtime, current, message, identity)

        try:
            await goals.dispatch_continuation_if_active(work.session_id, marker, dispatch,
                                                       goal_id=goal.goal_id, revision=goal.revision)
        except BaseException:
            runtime._goal_job_owners.update(owners)
            raise


async def sample_goal_progress(runtime: PersistentTaskRuntime, session_id: str) -> tuple[GoalProgressSample, ...]:
    """控制请求前只采样；冻结由原 Goal owner 在 CAS 提交中完成。"""
    samples = []
    for job in await runtime.jobs.list(session_id=session_id):
        owner = runtime._goal_job_owners.get(job.job_id)
        if owner is not None and job.status.value in {"accepted", "queued", "running", "streaming"}:
            samples.append(GoalProgressSample(owner, job.job_id,
                max(0, int((datetime.now(job.created_at.tzinfo) - job.created_at).total_seconds()))))
    return tuple(samples)
