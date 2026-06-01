from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import EventType, JobEventBus
from app.core.background_message_bus import BackgroundMessageBus
from app.schemas.common import MessageRole, RunMode
from app.schemas.message import MessageCreateRequest, MessageRunRequest, RunOptions
from app.schemas.session import SessionAutoContinueStatusDTO
from app.services.config_service import ConfigService
from app.services.job_service import JobService
from app.services.message_service import MessageService
from app.services.session_service import SessionService


@dataclass
class _AutoContinueState:
    session_id: str
    task_id: str
    started_at: datetime
    poll_interval_seconds: float
    forwarded_count: int = 0
    last_forwarded_at: Optional[datetime] = None
    last_trigger_event_id: Optional[str] = None
    last_trigger_job_id: Optional[str] = None
    last_enqueued_job_id: Optional[str] = None


class SessionAutoContinueService:
    def __init__(self):
        self._states: dict[str, _AutoContinueState] = {}
        self._lock = asyncio.Lock()
        self._background_task_registry: BackgroundTaskRegistry | None = None
        self._background_message_bus: BackgroundMessageBus | None = None
        self._job_event_bus: JobEventBus | None = None
        self._session_service: SessionService | None = None
        self._message_service: MessageService | None = None
        self._job_service: JobService | None = None
        self._config_service: ConfigService | None = None

    def bind_dependencies(
        self,
        *,
        background_task_registry: BackgroundTaskRegistry,
        background_message_bus: BackgroundMessageBus,
        job_event_bus: JobEventBus,
        session_service: SessionService,
        message_service: MessageService,
        job_service: JobService,
        config_service: ConfigService,
    ) -> None:
        self._background_task_registry = background_task_registry
        self._background_message_bus = background_message_bus
        self._job_event_bus = job_event_bus
        self._session_service = session_service
        self._message_service = message_service
        self._job_service = job_service
        self._config_service = config_service

    async def start(
        self,
        session_id: str,
        poll_interval_seconds: float = 1.0,
    ) -> SessionAutoContinueStatusDTO:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须大于 0")

        if self._session_service is None:
            raise RuntimeError("SessionAutoContinueService 未绑定 SessionService")
        await self._session_service.get(session_id)

        async with self._lock:
            existing = self._states.get(session_id)
            if existing is not None:
                if self._background_task_registry is None:
                    raise RuntimeError("SessionAutoContinueService 未绑定 BackgroundTaskRegistry")
                task = self._background_task_registry.get_task(session_id, existing.task_id)
                if task is not None and not task.done():
                    return self._to_status(existing, enabled=True)
                self._states.pop(session_id, None)

            started_at = datetime.now()
            state_holder: dict[str, _AutoContinueState] = {}

            async def _runner() -> None:
                await self._run_auto_continue_loop(state_holder["state"])

            if self._background_task_registry is None:
                raise RuntimeError("SessionAutoContinueService 未绑定 BackgroundTaskRegistry")
            handle = self._background_task_registry.spawn(
                session_id=session_id,
                task_name="managed_auto_continue",
                runner=_runner,
                metadata={
                    "poll_interval_seconds": poll_interval_seconds,
                    "started_at": started_at.isoformat(),
                    "session_id": session_id,
                },
            )

            state = _AutoContinueState(
                session_id=session_id,
                task_id=handle.task_id,
                started_at=started_at,
                poll_interval_seconds=poll_interval_seconds,
            )
            state_holder["state"] = state
            self._states[session_id] = state
            return self._to_status(state, enabled=True)

    async def stop(self, session_id: str, *, session_service: SessionService) -> SessionAutoContinueStatusDTO:
        if self._session_service is None:
            raise RuntimeError("SessionAutoContinueService 未绑定 SessionService")
        await self._session_service.get(session_id)

        state: _AutoContinueState | None = None
        task: asyncio.Task | None = None

        async with self._lock:
            state = self._states.get(session_id)
            if state is None:
                return SessionAutoContinueStatusDTO(
                    session_id=session_id,
                    enabled=False,
                    task_id=None,
                    task_status="stopped",
                    poll_interval_seconds=None,
                    started_at=None,
                    forwarded_count=0,
                    last_forwarded_at=None,
                    last_trigger_event_id=None,
                    last_trigger_job_id=None,
                    last_enqueued_job_id=None,
                )

            if self._background_task_registry is None:
                raise RuntimeError("SessionAutoContinueService 未绑定 BackgroundTaskRegistry")
            task = self._background_task_registry.get_task(session_id, state.task_id)
            self._states.pop(session_id, None)

        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        return self._to_status(state, enabled=False)

    async def get_status(self, session_id: str, *, session_service: SessionService) -> SessionAutoContinueStatusDTO:
        if self._session_service is None:
            raise RuntimeError("SessionAutoContinueService 未绑定 SessionService")
        await self._session_service.get(session_id)

        async with self._lock:
            state = self._states.get(session_id)
            if state is None:
                return SessionAutoContinueStatusDTO(
                    session_id=session_id,
                    enabled=False,
                    task_id=None,
                    task_status="stopped",
                    poll_interval_seconds=None,
                    started_at=None,
                    forwarded_count=0,
                    last_forwarded_at=None,
                    last_trigger_event_id=None,
                    last_trigger_job_id=None,
                    last_enqueued_job_id=None,
                )

            if self._background_task_registry is None:
                raise RuntimeError("SessionAutoContinueService 未绑定 BackgroundTaskRegistry")
            task = self._background_task_registry.get_task(session_id, state.task_id)
            if task is None or task.done():
                self._states.pop(session_id, None)
                return self._to_status(state, enabled=False)

            return self._to_status(state, enabled=True)

    async def _run_auto_continue_loop(self, state: _AutoContinueState) -> None:
        if self._job_event_bus is None:
            raise RuntimeError("SessionAutoContinueService 未绑定 JobEventBus")
        job_event_bus = self._job_event_bus
        if self._job_service is None:
            raise RuntimeError("SessionAutoContinueService 未绑定 JobService")
        if self._message_service is None:
            raise RuntimeError("SessionAutoContinueService 未绑定 MessageService")
        if self._config_service is None:
            raise RuntimeError("SessionAutoContinueService 未绑定 ConfigService")
        job_service = self._job_service
        seen_event_ids: set[str] = set()

        while True:
            jobs = await job_service.list(session_id=state.session_id)
            jobs.sort(key=lambda item: item.created_at)

            for job in jobs:
                events = await job_event_bus.list_events(job.job_id, limit=1000)
                events.sort(key=lambda item: item.timestamp)

                for event in events:
                    if event.event_id in seen_event_ids:
                        continue
                    seen_event_ids.add(event.event_id)

                    if event.type != EventType.AGENT_END:
                        continue
                    if event.timestamp <= state.started_at:
                        continue

                    await self._send_continue_message(state, trigger_job_id=job.job_id, trigger_event_id=event.event_id)

            await asyncio.sleep(state.poll_interval_seconds)

    async def _send_continue_message(self, state: _AutoContinueState, trigger_job_id: str, trigger_event_id: str) -> None:
        if self._session_service is None:
            raise RuntimeError("SessionAutoContinueService 未绑定 SessionService")
        if self._job_event_bus is None:
            raise RuntimeError("SessionAutoContinueService 未绑定 JobEventBus")
        session = await self._session_service.get(state.session_id)
        job_event_bus = self._job_event_bus
        run_request = MessageRunRequest(
            message=MessageCreateRequest(
                role=MessageRole.user,
                content="继续",
            ),
            run=RunOptions(
                mode=RunMode.single_agent,
                agent_id=session.current_agent_id,
            ),
        )

        result = await self._message_service.create_and_run(
            state.session_id,
            run_request,
            session_service=self._session_service,
            config_service=self._config_service,
            job_service=self._job_service,
            job_event_bus=job_event_bus,
        )
        state.forwarded_count += 1
        state.last_forwarded_at = datetime.now()
        state.last_trigger_job_id = trigger_job_id
        state.last_trigger_event_id = trigger_event_id
        state.last_enqueued_job_id = result.job_id

    def _to_status(self, state: _AutoContinueState, enabled: bool) -> SessionAutoContinueStatusDTO:
        task_status = "stopped"
        if enabled:
            if self._background_task_registry is None:
                raise RuntimeError("SessionAutoContinueService 未绑定 BackgroundTaskRegistry")
            handle = self._background_task_registry.get_handle(state.session_id, state.task_id)
            if handle is not None:
                task_status = handle.status
            else:
                task_status = "running"

        return SessionAutoContinueStatusDTO(
            session_id=state.session_id,
            enabled=enabled,
            task_id=state.task_id if enabled else None,
            task_status=task_status,
            poll_interval_seconds=state.poll_interval_seconds,
            started_at=state.started_at,
            forwarded_count=state.forwarded_count,
            last_forwarded_at=state.last_forwarded_at,
            last_trigger_event_id=state.last_trigger_event_id,
            last_trigger_job_id=state.last_trigger_job_id,
            last_enqueued_job_id=state.last_enqueued_job_id,
        )
