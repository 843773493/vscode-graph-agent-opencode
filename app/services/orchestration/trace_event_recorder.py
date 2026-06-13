from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.schemas.event import Event
from app.services.infrastructure.trace_event_store import TraceEventStore

logger = logging.getLogger(__name__)


class TraceEventRecorder:
    def __init__(self, *, bus: JobEventBusProtocol, store: TraceEventStore) -> None:
        self._bus = bus
        self._store = store
        self._job_sessions: dict[str, str] = {}
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        ready = asyncio.Event()
        self._task = asyncio.create_task(self._run(ready))
        await ready.wait()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self, ready: asyncio.Event) -> None:
        queue = await self._bus.subscribe_all()
        ready.set()
        try:
            while True:
                event = await queue.get()
                self._handle_event(event)
        finally:
            await self._bus.unsubscribe_all(queue)

    def _handle_event(self, event: Event) -> None:
        session_id = self._resolve_session_id(event)
        if not session_id:
            logger.warning("无法解析事件 session_id: event_id=%s type=%s job_id=%s", event.event_id, event.type, event.job_id)
            return

        if event.type == "job_created":
            self._job_sessions[event.job_id] = session_id

        self._store.append(session_id, event)

    def _resolve_session_id(self, event: Event) -> str | None:
        payload = event.payload

        if hasattr(payload, "session_id"):
            value = getattr(payload, "session_id")
            if isinstance(value, str) and value:
                return value

        raw = event.model_dump(mode="json")
        payload_raw = raw.get("payload") or {}
        for key in ("session_id", "thread_id"):
            value = payload_raw.get(key) or raw.get(key)
            if isinstance(value, str) and value:
                return value

        return self._job_sessions.get(event.job_id)
