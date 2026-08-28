"""Session 用户打断服务：取消正在运行的任务，并在进程内通知。"""
from __future__ import annotations

from datetime import UTC, datetime

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.core.turn_execution_scope import (
    TurnControlCoordinator,
    TurnExecutionScopeRegistry,
)
from app.schemas.internal_v2.common import ControlAction, JobStatus
from app.schemas.internal_v2.job import JobControlRequest
from app.schemas.internal_v2.session import SessionInterruptResultDTO
from app.services.business.message_service import MessageService
from app.services.business.system_reminder_checkpoint_service import (
    build_user_interrupt_reminder,
)
from app.services.infrastructure.message_stream_store import MessageStreamStore


class SessionInterruptService:
    def __init__(
        self,
        *,
        job_service: JobServiceProtocol,
        job_event_bus: JobEventBusProtocol,
        message_service: MessageService,
        message_stream_store: MessageStreamStore,
        execution_scope_registry: TurnExecutionScopeRegistry | None = None,
    ) -> None:
        self._job_service = job_service
        self._job_event_bus = job_event_bus
        self._message_service = message_service
        self._message_stream_store = message_stream_store
        self._execution_scope_registry = execution_scope_registry

    async def interrupt(self, session_id: str) -> SessionInterruptResultDTO:
        jobs = await self._job_service.list(session_id=session_id)
        active_job = next(
            (
                job
                for job in jobs
                if job.status
                in {
                    JobStatus.running,
                    JobStatus.streaming,
                    JobStatus.waiting_input,
                }
            ),
            None,
        )
        if active_job is None:
            raise ValueError(f"Session {session_id} 当前没有正在运行的任务")

        state = SessionInterruptState.get(session_id)
        phase = state.phase or "text"
        tool_name = state.tool_name
        current_text = state.current_text
        interrupted_at = datetime.now(UTC)
        interrupt_request_id = create_prefixed_id("intr")

        message_stream = await self._message_stream_store.open(
            session_id=session_id,
            turn_id=active_job.job_id,
            job_id=active_job.job_id,
        )
        control_inbox = (
            self._execution_scope_registry.get_inbox(message_stream.turn_stream_id)
            if self._execution_scope_registry is not None
            else None
        )
        scope = (
            self._execution_scope_registry.get(message_stream.turn_stream_id)
            if self._execution_scope_registry is not None
            else None
        )
        if control_inbox is not None and scope is not None:
            interrupt_event = await TurnControlCoordinator(
                scope=scope,
                inbox=control_inbox,
                writer=message_stream,
            ).submit_interrupt(
                command_id=interrupt_request_id,
                idempotency_key=interrupt_request_id,
            )
        else:
            # AgentLoop 尚未完成 scope 注册时仍需先持久化控制事实；后续
            # AgentExecutionService 会从消息流终态判断这次早期中断。
            interrupt_event = await message_stream.commit(
                "interrupt.requested",
                {
                    "interrupt_request_id": interrupt_request_id,
                    "reason": "user_requested",
                },
                event_id=interrupt_request_id,
            )
        if interrupt_event["type"] == "interrupt.rejected":
            stream_state = await self._message_stream_store.get_state(
                message_stream.turn_stream_id
            )
            return SessionInterruptResultDTO(
                session_id=session_id,
                job_id=active_job.job_id,
                status=str(stream_state["stream_status"]),
                interrupt_request_id=interrupt_request_id,
                phase=phase,
                tool_name=tool_name,
                interrupted_at=interrupted_at,
            )
        SessionInterruptState.set(
            session_id,
            interrupt_request_id=interrupt_request_id,
            cancellation_reason="user_requested",
        )
        if control_inbox is None and self._execution_scope_registry is not None:
            await self._execution_scope_registry.cancel(
                message_stream.turn_stream_id,
                "user_requested",
            )

        reminder_injected = self._append_user_interrupt_reminder(
            session_id=session_id,
            phase=phase,
            tool_name=tool_name,
            current_text=current_text,
            interrupted_at=interrupted_at,
        )
        if reminder_injected:
            SessionInterruptState.set(
                session_id,
                user_interrupt_reminder_injected=True,
            )

        await self._job_service.control(
            active_job.job_id,
            JobControlRequest(action=ControlAction.cancel),
        )

        await self._job_service.notify_boundary(
            session_id,
            "after_interrupt",
            tool_result_available=False,
        )

        if self._job_event_bus is not None:
            await self._job_event_bus.publish(
                job_id=active_job.job_id,
                event_type=EventType.SESSION_INTERRUPTED,
                payload={
                    "session_id": session_id,
                    "phase": phase,
                    "tool_name": tool_name,
                    "interrupted_at": interrupted_at.isoformat(),
                    "interrupt_request_id": interrupt_request_id,
                },
                agent_id="session_interrupt_service",
            )

        return SessionInterruptResultDTO(
            session_id=session_id,
            job_id=active_job.job_id,
            status=JobStatus.cancelling.value,
            interrupt_request_id=interrupt_request_id,
            phase=phase,
            tool_name=tool_name,
            interrupted_at=interrupted_at,
        )

    def _append_user_interrupt_reminder(
        self,
        *,
        session_id: str,
        phase: str,
        tool_name: str | None,
        current_text: str,
        interrupted_at: datetime,
    ) -> bool:
        reminder = build_user_interrupt_reminder(
            phase=phase,
            active_tool_name=tool_name,
            interrupted_at=interrupted_at,
        )
        assistant_text = current_text if phase == "text" else ""
        metadata: dict[str, object] = {
            "phase": phase,
            "tool_name": tool_name,
            "source": "user_interrupt",
            "user_initiated": True,
            "interrupted_at": interrupted_at.isoformat(),
        }
        injected = self._message_service.append_system_reminder(
            session_id=session_id,
            reminder=reminder,
            response_metadata=metadata,
            assistant_text=assistant_text,
            assistant_response_metadata=metadata,
            checkpoint_source="user_interrupt",
        )
        if not injected:
            raise RuntimeError(
                f"用户主动取消时未找到可注入 system_reminder 的 checkpoint: "
                f"session_id={session_id} phase={phase} tool={tool_name}"
            )
        return injected
