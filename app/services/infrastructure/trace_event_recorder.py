from __future__ import annotations

from typing import Any

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.trace_event_sink import TraceEventSinkProtocol
from app.schemas.event import Event


class TraceEventRecorder:
    """将 JobEventBus 的事件同步写入权威会话 trace。"""

    def __init__(
        self,
        *,
        bus: JobEventBusProtocol,
        store: TraceEventSinkProtocol,
    ) -> None:
        self._bus = bus
        self._store = store
        self._job_sessions: dict[str, str] = {}
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self._bus.register_durable_listener(self._handle_event)
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        await self._bus.unregister_durable_listener(self._handle_event)
        self._started = False

    async def _handle_event(self, event: Event) -> None:
        session_id = self._resolve_session_id(event)
        if not session_id:
            raise RuntimeError(
                "无法持久化缺少 session_id 的事件: "
                f"event_id={event.event_id} type={event.type} job_id={event.job_id}"
            )

        await self._store.append(session_id, event)
        if event.type == "job_created":
            self._job_sessions[event.job_id] = session_id

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

        mapped = self._job_sessions.get(event.job_id)
        if mapped:
            return mapped

        # Agent Runtime 的 fallback job_id 可以直接等于 session_id。
        if event.job_id.startswith("ses_"):
            return event.job_id

        return None
