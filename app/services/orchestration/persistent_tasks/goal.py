"""Goal 应用的一次工作；排队、并发、恢复及关闭由通用 worker 持有。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from app.abstractions.internal_message import PreparedInternalMessage
from app.schemas.internal_v2.goal import GoalStatus, SessionGoalDTO
from app.schemas.internal_v2.job import JobDTO
from app.services.business.persistent_tasks.goal import (
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
    goal: SessionGoalDTO | None = None
    request_id: str | None = None
    gateway_id: str | None = None
    observed_at: datetime | None = None
    recovering: bool = False


async def _submit(runtime: PersistentTaskRuntime, goal: SessionGoalDTO,
                  message: PreparedInternalMessage, identity: str) -> None:
    await runtime._orchestrator.submit_runtime_event(
        goal.session_id, message, thread_id=goal.thread_id, source_id=f"goal:{goal.goal_id}:state",
        idempotency_key=f"goal:{goal.goal_id}:{identity}:{message.metadata['structured_prompt_kind']}",
        delivery_policy="after_turn",
    )


def _metadata(goal: SessionGoalDTO) -> dict[str, object]:
    return {"goal_continuation": True, "goal_id": goal.goal_id, "goal_objective": goal.objective}


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
        if work.kind != "agent_end":
            if goal.status == GoalStatus.active:
                await goals.set(work.session_id, status=GoalStatus.paused if work.kind == "cancelled" else GoalStatus.blocked)
            return
        assert work.observed_at is not None
        elapsed = max(0, int((job.ended_at or work.observed_at).timestamp() - job.created_at.timestamp()))
        goal = await goals.account_job(work.session_id, job_id=job_id, tokens=work.tokens, elapsed_seconds=elapsed)
        if goal is None:
            return
        async with runtime.action_admission() as admitted:
            if not admitted:
                return
            if goal.status == GoalStatus.budget_limited:
                await _submit(runtime, goal, budget_limited_message(goal, metadata={**_metadata(goal), "goal_budget_limited": True}), job_id)
                return

            async def dispatch(current: SessionGoalDTO) -> None:
                await _submit(runtime, current, continuation_message(current, metadata=_metadata(current)), job_id)

            await goals.dispatch_continuation_if_active(work.session_id, job_id, dispatch)
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

        async def dispatch(current: SessionGoalDTO) -> None:
            message = (objective_updated_message(current, metadata={**_metadata(current), "goal_objective_updated": True})
                       if objective else continuation_message(current, metadata=_metadata(current)))
            await _submit(runtime, current, message, identity)

        try:
            await goals.dispatch_continuation_if_active(work.session_id, marker, dispatch)
        except BaseException:
            runtime._goal_job_owners.update(owners)
            raise


async def settle_goal_progress(runtime: PersistentTaskRuntime, session_id: str) -> None:
    """外部暂停/替换/清除前冻结在途用时；不触发调度或等待工作执行。"""
    goal = await runtime.goals.get(session_id)
    if goal is None:
        return
    for job in await runtime.jobs.list(session_id=session_id):
        if job.status.value not in {"accepted", "queued", "running", "streaming"} or runtime._goal_job_owners.get(job.job_id) != goal.goal_id:
            continue
        await runtime.goals.account_job(
            session_id, job_id=job.job_id, tokens=0,
            elapsed_seconds=max(0, int((datetime.now(job.created_at.tzinfo) - job.created_at).total_seconds())), close_time=True,
        )
