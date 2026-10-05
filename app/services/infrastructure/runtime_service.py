from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

from app.abstractions.state_conflict import ClientStateConflictError
from app.abstractions.turn_terminal_status import TurnTerminalStatusWriter
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.identifier import create_prefixed_id
from app.core.workspace_identity import validate_workspace_id
from app.schemas.event import Event, SessionInterruptedEvent, SessionInterruptedPayload
from app.schemas.internal_v2.runtime import (
    RuntimeDrainBlockerDTO,
    RuntimeDrainResultDTO,
    RuntimeInfoDTO,
    RuntimeLifecycleState,
    RuntimeStorageDTO,
)
from app.services.business.job.service import JobService
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
    ) -> None:
        self._workspace_id = validate_workspace_id(workspace_id)
        self._workspace_root = workspace_root.expanduser().resolve()
        self._job_service = job_service
        self._background_task_registry = background_task_registry
        self._trace_event_store = trace_event_store
        self._message_stream_store = message_stream_store
        self._terminal_status_writer = terminal_status_writer
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
            storage=RuntimeStorageDTO(
                root=str(self._workspace_root),
                artifact_dir=str(self._workspace_root / ".boxteam" / "artifacts"),
                log_dir=str(self._workspace_root / ".boxteam" / "logs"),
                cache_dir=str(self._workspace_root / ".boxteam" / "cache"),
            ),
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
        """把上次进程未写终态的已启动 Job 标为进程中断。"""
        self._startup_reconciliation_errors = []
        reconciled = await self._message_stream_store.reconcile_unfinished_streams()
        terminal_types = {
            "job_completed",
            "job_cancelled",
            "job_failed",
            "session_interrupted",
        }
        started_owners: dict[str, tuple[str, str]] = {}
        for session_id in self._trace_event_store.list_session_ids():
            events = self._trace_event_store.read_events(session_id)
            lifecycle_by_job: dict[str, Event] = {}
            for event in events:
                if event.type == "job_started":
                    owner = (event.payload.session_id, event.payload.thread_id)
                    if owner[0] != session_id:
                        raise RuntimeError(
                            f"Job started owner 与 Trace 会话冲突: job_id={event.job_id!r}, "
                            f"trace_session={session_id!r}, owner={owner!r}"
                        )
                    previous = started_owners.setdefault(event.job_id, owner)
                    if previous != owner:
                        raise RuntimeError(
                            f"同一 Job 的 started owner 漂移: job_id={event.job_id!r}, "
                            f"previous={previous!r}, current={owner!r}"
                        )
                if event.type == "job_started" or event.type in terminal_types:
                    lifecycle_by_job[event.job_id] = event
            for job_id, event in lifecycle_by_job.items():
                owner = started_owners.get(job_id)
                # 排队取消或准入前失败可以没有 started，不产生 execution/Turn。
                if owner is None:
                    continue
                if owner[0] != session_id:
                    raise RuntimeError(
                        f"Job 终态 Trace 与 started owner 冲突: job_id={job_id!r}, "
                        f"trace_session={session_id!r}, owner={owner!r}"
                    )
                event_type = event.type
                if event_type == "job_failed":
                    status = "timed_out" if is_job_timeout_event(event) else "failed"
                elif event_type == "job_cancelled":
                    status = "cancelled"
                elif event_type == "session_interrupted":
                    payload = event.payload
                    if payload.code != "execution_lost" and payload.phase != "process_exit":
                        continue
                    status = "execution_lost"
                elif event_type == "job_started":
                    status = "execution_lost"
                else:
                    continue
                if self._terminal_status_writer is not None:
                    self._terminal_status_writer.mark_job_terminal_status(
                        session_id=owner[0], thread_id=owner[1],
                        job_id=job_id, status=status,
                    )
                if event_type != "job_started":
                    continue
                now = datetime.now(UTC)
                await self._trace_event_store.append(
                    session_id,
                    SessionInterruptedEvent(
                        event_id=create_prefixed_id("evt"),
                        job_id=job_id,
                        agent_id="runtime_reconciler",
                        timestamp=now,
                        payload=SessionInterruptedPayload(
                            session_id=session_id,
                            phase="process_exit",
                            interrupted_at=now,
                            code="execution_lost",
                            message="工作区后端重启，无法安全续接原 AgentLoop 执行",
                            resumable=False,
                        ),
                    ),
                )
                reconciled += 1
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
