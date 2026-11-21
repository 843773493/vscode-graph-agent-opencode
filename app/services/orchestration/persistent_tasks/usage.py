"""通过同一 canonical 执行引用结算任务；事件只负责唤醒，不承担事实存储。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from app.domain.itemized.runtime_wakeup import RuntimeExecutionProgress
from app.schemas.internal_v2.task_usage import TaskExecutionReferenceDTO

if TYPE_CHECKING:
    from app.services.orchestration.persistent_tasks.runtime import (
        PersistentTaskRuntime,
    )


def bind_execution(runtime: PersistentTaskRuntime, task_id: str, *, session_id: str, thread_id: str,
                   job_id: str, control_revision: int, run_id: str | None = None,
                   role: Literal["work", "evaluation", "report", "notice"] = "work") -> TaskExecutionReferenceDTO:
    record = runtime._saver.execution_queue_record(session_id, thread_id, job_id)
    reference = TaskExecutionReferenceDTO(
        session_id=session_id, thread_id=thread_id, execution_id=record.cursor.admission.execution_id,
        started_at=datetime.fromisoformat(record.input.created_at), control_revision=control_revision, run_id=run_id, role=role,
    )
    # Job 可能早于动作回执写入开始。重复登记沿用第一次冻结的控制版本和用途。
    state = runtime.store.usage_state(task_id)
    previous = state.accounted_jobs.get(job_id)
    if previous is not None and previous.execution is not None:
        original = previous.execution
        if (original.session_id, original.thread_id, original.execution_id) != (session_id, thread_id, reference.execution_id):
            raise RuntimeError("任务执行归属与 canonical 准入冲突")
        return original
    runtime.store.bind_execution(task_id, job_id, reference, runtime.clock())
    return reference


async def execution_sample(runtime: PersistentTaskRuntime, job_id: str,
                           reference: TaskExecutionReferenceDTO) -> tuple[RuntimeExecutionProgress, int]:
    async with runtime._sessions.session_read_admission(reference.session_id):
        runtime._sessions.path_resolver.resolve_thread_node(reference.session_id, reference.thread_id)
        progress = runtime._saver.execution_progress(reference.session_id, reference.thread_id,
            job_id=job_id, execution_id=reference.execution_id)
    elapsed = max(0, int(datetime.fromisoformat(progress.updated_at).timestamp() - reference.started_at.timestamp()))
    return progress, elapsed


async def settle_execution(runtime: PersistentTaskRuntime, task_id: str, job_id: str,
                           reference: TaskExecutionReferenceDTO) -> str:
    progress, elapsed = await execution_sample(runtime, job_id, reference)
    settled = progress.outcome != "unknown"
    if runtime.store.account_execution(task_id, job_id=job_id, tokens=progress.total_tokens,
            elapsed_seconds=elapsed, settled=settled, now=runtime.clock()):
        runtime.notify(task_id)
    return progress.outcome


async def settle_task(runtime: PersistentTaskRuntime, task_id: str, jobs: set[str]) -> None:
    for job_id, accounting in runtime.store.usage_state(task_id).accounted_jobs.items():
        if accounting.execution is not None and (not accounting.settled or job_id in jobs):
            await settle_execution(runtime, task_id, job_id, accounting.execution)
