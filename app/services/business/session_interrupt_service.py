"""Session 用户打断服务：将打断状态写入 checkpoint，由 middleware 注入 system_reminder。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.core.checkpoint_saver import next_channel_version
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
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self._job_service = job_service
        self._job_event_bus = job_event_bus
        self._checkpointer = checkpointer

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

        await self._write_interrupt_checkpoint(
            session_id,
            phase=phase,
            tool_name=tool_name,
            interrupted_at=interrupted_at,
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

    async def _write_interrupt_checkpoint(
        self,
        session_id: str,
        *,
        phase: str,
        tool_name: str | None,
        interrupted_at: datetime,
    ) -> None:
        if self._checkpointer is None:
            return

        config: dict[str, Any] = {
            "configurable": {
                "thread_id": session_id,
                "checkpoint_ns": "",
            }
        }
        tup = await self._checkpointer.aget_tuple(config)
        if tup is None:
            return

        checkpoint = tup.checkpoint.copy()
        checkpoint["id"] = str(uuid.uuid4())
        channel_values = dict(checkpoint.get("channel_values", {}))
        channel_values["__boxteam_interrupt__"] = {
            "phase": phase,
            "tool_name": tool_name,
            "interrupted_at": interrupted_at.isoformat(),
        }
        checkpoint["channel_values"] = channel_values

        channel_versions = dict(checkpoint.get("channel_versions", {}))
        interrupt_version = next_channel_version(channel_versions.get("__boxteam_interrupt__"))
        channel_versions["__boxteam_interrupt__"] = interrupt_version
        checkpoint["channel_versions"] = channel_versions

        updated_channels = list(checkpoint.get("updated_channels", []))
        if "__boxteam_interrupt__" not in updated_channels:
            updated_channels.append("__boxteam_interrupt__")
        checkpoint["updated_channels"] = updated_channels

        # metadata 用空 writes 占位
        metadata = {"source": "interrupt", "step": -1, "writes": {}}
        await self._checkpointer.aput(
            config=tup.config,
            checkpoint=checkpoint,
            metadata=metadata,
            new_versions={"__boxteam_interrupt__": interrupt_version},
        )
