from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from app.abstractions.pending_request_store import PendingRequestStoreProtocol
from app.abstractions.state_conflict import ClientStateConflictError
from app.abstractions.turn_terminal_status import TurnTerminalStatusWriter
from app.agents.checkpoint_recovery import CheckpointRecoveryRejectedError
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.identifier import create_prefixed_id
from app.core.workspace_identity import validate_workspace_id
from app.schemas.event import (
    Event,
    SessionInterruptedEvent,
    SessionInterruptedPayload,
)
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.runtime import (
    RuntimeDrainBlockerDTO,
    RuntimeDrainResultDTO,
    RuntimeInfoDTO,
    RuntimeLifecycleState,
)
from app.services.business.job.service import JobService, JobState
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.trace_event_store import TraceEventStore


def is_job_timeout_event(event: Event) -> bool:
    """Job 的两类超时由唯一结构化 code 判定。"""
    return getattr(event.payload, "code", None) in {"job_timeout", "job_startup_timeout"}


class RuntimeService:
    def __init__(
        self,
        *,
        workspace_id: str,
        workspace_root: Path,
        job_service: JobService,
        background_task_registry: BackgroundTaskRegistry,
        trace_event_store: TraceEventStore,
        message_stream_store: MessageStreamStore,
        terminal_status_writer: TurnTerminalStatusWriter | None = None,
        pending_tool_checkpoint_id: Callable[[JobState], Awaitable[str | None]],
        pending_request_store: PendingRequestStoreProtocol,
    ) -> None:
        self._workspace_id = validate_workspace_id(workspace_id)
        self._workspace_root = workspace_root.expanduser().resolve()
        self._job_service = job_service
        self._background_task_registry = background_task_registry
        self._trace_event_store = trace_event_store
        self._message_stream_store = message_stream_store
        self._terminal_status_writer = terminal_status_writer
        self._pending_tool_checkpoint_id = pending_tool_checkpoint_id
        self._pending_request_store = pending_request_store
        self._lifecycle_state: RuntimeLifecycleState = "ready"
        self._start_time: float | None = None
        self._startup_reconciliation_errors: list[dict[str, str]] = []

    def get_log_dir(self) -> Path:
        return self._workspace_root / ".boxteam" / "logs"

    @property
    def startup_reconciliation_errors(self) -> list[dict[str, str]]:
        """返回启动恢复时发现的历史 Trace 错误，供健康检查暴露。"""
        return list(self._startup_reconciliation_errors)

    async def status(self) -> RuntimeInfoDTO:
        if self._start_time is None:
            self._start_time = time.time()

        blockers = await self._blockers()
        return RuntimeInfoDTO(
            pid=os.getpid(),
            uptime_seconds=int(time.time() - self._start_time),
            workspace_id=self._workspace_id,
            active_jobs=sum(1 for blocker in blockers if blocker.kind == "job"),
            lifecycle_state=self._lifecycle_state,
            accepting_jobs=self._job_service.accepting_jobs,
            blockers=blockers,
            loaded_agents=["planner", "executor", "reviewer", "summarizer"],
        )

    async def begin_drain(self) -> RuntimeDrainResultDTO:
        if self._lifecycle_state == "stopping":
            raise ClientStateConflictError(
                "Workspace API 已进入 stopping，不能重新开始 drain"
            )
        self._job_service.close_admission()
        self._lifecycle_state = "draining"
        return await self._drain_result()

    async def cancel_drain(self) -> RuntimeDrainResultDTO:
        if self._lifecycle_state != "draining":
            raise ClientStateConflictError(
                f"只有 draining 状态可以取消排空，当前状态: {self._lifecycle_state}"
            )
        self._job_service.open_admission()
        self._lifecycle_state = "ready"
        return await self._drain_result()

    async def force_interrupt(self) -> RuntimeDrainResultDTO:
        if self._lifecycle_state != "draining":
            raise ClientStateConflictError(
                f"强制中断前必须先进入 draining，当前状态: {self._lifecycle_state}"
            )
        reason = "Gateway 显式强制重启 Workspace API"
        interrupted_jobs = await self._job_service.force_interrupt_active(reason=reason)
        interrupted_background = (
            await self._background_task_registry.cancel_all_active(reason=reason)
        )
        self._lifecycle_state = "stopping"
        result = await self._drain_result()
        result.interrupted_resources = interrupted_jobs + interrupted_background
        return result

    async def reconcile_stale_executions(self) -> int:
        """保留首个 Provider 前的原 runtime 准入，恢复安全 checkpoint，其余执行标为丢失。"""
        self._startup_reconciliation_errors = []
        reconciled = 0
        resumed_job_ids: set[str] = set()
        terminal_types = {
            "job_completed",
            "job_cancelled",
            "job_failed",
            "session_interrupted",
        }
        job_owners: dict[str, tuple[str, str]] = {}
        lifecycle_jobs_by_session: dict[str, set[str]] = {}
        event: Event | None = None
        for session_id in self._trace_event_store.list_session_ids():
            lifecycle_jobs: set[str] = set()
            for event in self._trace_event_store.read_events(session_id):
                if event.type in {"job_created", "job_started"}:
                    owner = (event.payload.session_id, event.payload.thread_id)
                    phase = event.type.removeprefix("job_")
                    if owner[0] != session_id:
                        raise RuntimeError(
                            f"Job {phase} owner 与 Trace 会话冲突: job_id={event.job_id!r}, "
                            f"trace_session={session_id!r}, owner={owner!r}"
                        )
                    previous = job_owners.setdefault(event.job_id, owner)
                    if previous != owner:
                        raise RuntimeError(
                            f"同一 Job 的 {phase} owner 漂移: job_id={event.job_id!r}, "
                            f"previous={previous!r}, current={owner!r}"
                        )
                if event.type == "job_started" or event.type in terminal_types:
                    lifecycle_jobs.add(event.job_id)
            lifecycle_jobs_by_session[session_id] = lifecycle_jobs
            event = None

        # 先校验全部 Trace 的 owner，再调用恢复端口或写入终态，避免跨会话冲突留下部分副作用。
        for session_id, lifecycle_jobs in lifecycle_jobs_by_session.items():
            for job_id in lifecycle_jobs:
                owner = job_owners.get(job_id)
                if owner is not None and owner[0] != session_id:
                    raise RuntimeError(
                        f"Job 终态 Trace 与 started owner 冲突: job_id={job_id!r}, "
                        f"trace_session={session_id!r}, owner={owner!r}"
                    )

        records = await self._pending_request_store.records()
        for record in records:
            admission = record.cursor.admission
            owner = (admission.session_id, admission.thread_id)
            traced_owner = job_owners.get(admission.job_id)
            if traced_owner is not None and traced_owner != owner:
                raise RuntimeError(f"执行队列与 Trace owner 冲突: job_id={admission.job_id}, queue={owner}, trace={traced_owner}")
        for record in records:
            if record.phase == "queued":
                continue
            admission = record.cursor.admission
            claim = await self._pending_request_store.claim(record)
            if claim is None:
                # 本地工作区没有备用 runtime 接管协议；不能跳过队首后宣告启动成功。
                raise RuntimeError(
                    "工作区执行仍由另一个 runtime 持有，拒绝并存启动: "
                    f"session_id={admission.session_id}, thread_id={admission.thread_id}, "
                    f"job_id={admission.job_id}；请先停止原 runtime 后重试"
                )
            restored = False
            try:
                request = await self._pending_request_store.project(claim.record, position=0, snapshot_version=0)
                job = JobState(
                    job_id=admission.job_id, session_id=admission.session_id, thread_id=admission.thread_id,
                    message=request.content, message_id=request.message_id, message_created_at=request.message_created_at,
                    execution_binding=request.execution_binding, agent_id=request.agent_id,
                    status=JobStatus.running, message_metadata=dict(request.message_metadata), attachments=list(request.attachments),
                    gateway_id=request.gateway_id, request_id=request.request_id,
                    created_at=request.created_at, updated_at=request.updated_at, delivery_policy=request.delivery_policy,
                    queue_record=claim.record, queue_claim=claim,
                )
                if not await self._pending_request_store.before_dispatch(claim.record):
                    try:
                        job.resume_checkpoint_id = await self._pending_tool_checkpoint_id(job)
                    except CheckpointRecoveryRejectedError as error:
                        self._startup_reconciliation_errors.append({
                            "session_id": job.session_id, "job_id": job.job_id,
                            "error_type": type(error).__name__, "message": str(error),
                        })
                    if job.resume_checkpoint_id is None:
                        if self._terminal_status_writer is None:
                            raise RuntimeError("无法安全恢复的执行缺少 canonical terminal owner")
                        await self._message_stream_store.reconcile_job_tools(
                            session_id=admission.session_id, thread_id=admission.thread_id, job_id=job.job_id,
                            terminal_status_writer=self._terminal_status_writer,
                        )
                        if not self._terminal_status_writer.mark_job_terminal_status(
                            session_id=admission.session_id, thread_id=admission.thread_id,
                            job_id=job.job_id, status="execution_lost",
                        ):
                            raise RuntimeError("原持久执行缺少 terminal admission")
                        now = datetime.now(UTC)
                        await self._trace_event_store.append(admission.session_id, SessionInterruptedEvent(
                            event_id=create_prefixed_id("evt"), job_id=job.job_id, agent_id="runtime_reconciler", timestamp=now,
                            payload=SessionInterruptedPayload(
                                session_id=admission.session_id, phase="process_exit", interrupted_at=now,
                                code="execution_lost", message="工作区后端重启，无法安全续接原 AgentLoop 执行", resumable=False,
                            ),
                        ))
                        reconciled += 1
                        continue
                await self._job_service.restore_active_job(job)
                resumed_job_ids.add(job.job_id)
                restored = True
            finally:
                if not restored:
                    await claim.close()
        reconciled += await self._message_stream_store.reconcile_unfinished_streams(
            resumed_job_ids=resumed_job_ids,
        )
        return reconciled

    async def _blockers(self) -> list[RuntimeDrainBlockerDTO]:
        blockers: list[RuntimeDrainBlockerDTO] = []
        for job in await self._job_service.drain_blockers():
            blockers.append(
                RuntimeDrainBlockerDTO(
                    kind="job",
                    resource_id=job.job_id,
                    session_id=job.session_id,
                    status=job.status.value,
                    detail=job.phase,
                )
            )
            for tool_name in job.tool_names:
                blockers.append(
                    RuntimeDrainBlockerDTO(
                        kind="tool",
                        resource_id=f"{job.job_id}:{tool_name}",
                        session_id=job.session_id,
                        status="running",
                        detail=tool_name,
                    )
                )
        for task in self._background_task_registry.list_active_handles():
            blockers.append(
                RuntimeDrainBlockerDTO(
                    kind="background_task",
                    resource_id=task.task_id,
                    session_id=task.session_id,
                    status=task.status,
                    detail=task.task_name,
                )
            )
        return blockers

    async def _drain_result(self) -> RuntimeDrainResultDTO:
        return RuntimeDrainResultDTO(
            lifecycle_state=self._lifecycle_state,
            accepting_jobs=self._job_service.accepting_jobs,
            blockers=await self._blockers(),
        )
