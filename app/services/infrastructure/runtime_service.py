from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Protocol

from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.identifier import create_prefixed_id
from app.core.path_utils import (
    get_artifacts_dir,
    get_cache_dir,
    get_logs_dir,
    get_workspace_root,
)
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


class TurnTerminalStatusWriter(Protocol):
    """启动恢复时把仍处于 running 的持久化 Turn 收束为失败。"""

    def mark_turn_terminal_status(
        self,
        *,
        session_id: str,
        turn_id: str,
        status: str,
    ) -> bool: ...


def is_job_timeout_event(event: Event) -> bool:
    """兼容带 code 与旧版仅保存超时错误文本的 JOB_FAILED 事件。"""
    payload = event.payload
    if getattr(payload, "code", None) == "job_timeout":
        return True
    error = getattr(payload, "error", None)
    return isinstance(error, str) and "超过总超时上限" in error


class RuntimeService:
    _start_time = None

    def __init__(
        self,
        *,
        job_service: JobService,
        background_task_registry: BackgroundTaskRegistry,
        trace_event_store: TraceEventStore,
        message_stream_store: MessageStreamStore,
        terminal_status_writer: TurnTerminalStatusWriter | None = None,
    ) -> None:
        self._job_service = job_service
        self._background_task_registry = background_task_registry
        self._trace_event_store = trace_event_store
        self._message_stream_store = message_stream_store
        self._terminal_status_writer = terminal_status_writer
        self._lifecycle_state: RuntimeLifecycleState = "ready"

    def get_log_dir(self):
        return get_workspace_root() / ".boxteam" / "logs"

    async def status(self) -> RuntimeInfoDTO:
        if self._start_time is None:
            self._start_time = time.time()

        blockers = await self._blockers()
        return RuntimeInfoDTO(
            pid=os.getpid(),
            uptime_seconds=int(time.time() - self._start_time),
            workspace_id="ws_local",
            active_jobs=sum(1 for blocker in blockers if blocker.kind == "job"),
            lifecycle_state=self._lifecycle_state,
            accepting_jobs=self._job_service.accepting_jobs,
            blockers=blockers,
            loaded_agents=["planner", "executor", "reviewer", "summarizer"],
            storage=RuntimeStorageDTO(
                root=str(get_workspace_root()),
                artifact_dir=str(get_artifacts_dir()),
                log_dir=str(get_logs_dir()),
                cache_dir=str(get_cache_dir()),
            ),
        )

    async def begin_drain(self) -> RuntimeDrainResultDTO:
        if self._lifecycle_state == "stopping":
            raise RuntimeError("Workspace API 已进入 stopping，不能重新开始 drain")
        self._job_service.close_admission()
        self._lifecycle_state = "draining"
        return await self._drain_result()

    async def cancel_drain(self) -> RuntimeDrainResultDTO:
        if self._lifecycle_state != "draining":
            raise RuntimeError(
                f"只有 draining 状态可以取消排空，当前状态: {self._lifecycle_state}"
            )
        self._job_service.open_admission()
        self._lifecycle_state = "ready"
        return await self._drain_result()

    async def force_interrupt(self) -> RuntimeDrainResultDTO:
        if self._lifecycle_state != "draining":
            raise RuntimeError(
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
        reconciled = await self._message_stream_store.reconcile_unfinished_streams()
        terminal_types = {
            "job_completed",
            "job_cancelled",
            "job_failed",
            "session_interrupted",
        }
        for session_id in self._trace_event_store.list_session_ids():
            events = self._trace_event_store.read_events(session_id)
            lifecycle_by_job: dict[str, Event] = {}
            for event in events:
                if event.type == "job_started" or event.type in terminal_types:
                    lifecycle_by_job[event.job_id] = event
            for job_id, event in lifecycle_by_job.items():
                event_type = event.type
                if event_type == "job_failed":
                    if self._terminal_status_writer is not None:
                        status = (
                            "timed_out"
                            if is_job_timeout_event(event)
                            else "failed"
                        )
                        self._terminal_status_writer.mark_turn_terminal_status(
                            session_id=session_id,
                            turn_id=job_id,
                            status=status,
                        )
                    continue
                if event_type == "session_interrupted":
                    payload = event.payload
                    is_process_exit = (
                        payload.code == "execution_lost"
                        or payload.phase == "process_exit"
                    )
                    if is_process_exit and self._terminal_status_writer is not None:
                        self._terminal_status_writer.mark_turn_terminal_status(
                            session_id=session_id,
                            turn_id=job_id,
                            status="failed",
                        )
                    continue
                if event_type != "job_started":
                    continue
                if self._terminal_status_writer is not None:
                    self._terminal_status_writer.mark_turn_terminal_status(
                        session_id=session_id,
                        turn_id=job_id,
                        status="failed",
                    )
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
