"""Session 用户打断服务：取消正在运行的任务，并在进程内通知。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.schemas.public_v2.common import ControlAction, JobStatus
from app.schemas.public_v2.job import JobControlRequest
from app.schemas.public_v2.session import SessionInterruptResultDTO
from app.services.business.message_service import MessageService
from app.services.business.system_reminder_checkpoint_service import (
    build_user_interrupt_reminder,
)


class SessionInterruptService:
    def __init__(
        self,
        *,
        job_service: JobServiceProtocol,
        job_event_bus: JobEventBusProtocol,
        message_service: MessageService,
    ) -> None:
        self._job_service = job_service
        self._job_event_bus = job_event_bus
        self._message_service = message_service

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
        interrupted_at = datetime.now(timezone.utc)

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

        if self._job_event_bus is not None:
            await self._job_event_bus.publish(
                job_id=active_job.job_id,
                event_type=EventType.SESSION_INTERRUPTED,
                payload={
                    "session_id": session_id,
                    "phase": phase,
                    "tool_name": tool_name,
                    "interrupted_at": interrupted_at.isoformat(),
                },
                agent_id="session_interrupt_service",
            )

        return SessionInterruptResultDTO(
            session_id=session_id,
            job_id=active_job.job_id,
            status=JobStatus.cancelling.value,
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
