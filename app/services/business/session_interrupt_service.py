"""Session 用户打断服务：取消正在运行的任务，并在进程内通知。

实现说明：
    取消信号由 asyncio task.cancel() 发出；任务内的 CancelledError 处理路径
    （`AgentExecutionService._persist_interrupt_checkpoint`）负责把已生成的部分
    消息和 `<system_reminder>` 拼到 messages 末尾。LangGraph 下次 checkpoint 加载
    时，模型直接看到 reminder，无需再写中间 marker channel。
"""
from __future__ import annotations

from datetime import datetime

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.schemas.public_v2.common import ControlAction, JobStatus
from app.schemas.public_v2.job import JobControlRequest
from app.schemas.public_v2.session import SessionInterruptResultDTO


class SessionInterruptService:
    def __init__(
        self,
        *,
        job_service: JobServiceProtocol,
        job_event_bus: JobEventBusProtocol,
    ) -> None:
        self._job_service = job_service
        self._job_event_bus = job_event_bus

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
        interrupted_at = datetime.now()

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
