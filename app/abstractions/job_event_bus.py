from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from app.schemas.event import Event


@runtime_checkable
class JobEventBusProtocol(Protocol):
    async def publish(
        self,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
        step_id: str | None = None,
        agent_id: str | None = None,
    ) -> Event: ...

    async def subscribe(self, job_id: str) -> asyncio.Queue[Event]: ...

    async def unsubscribe(self, job_id: str, queue: asyncio.Queue[Event]) -> None: ...

    async def subscribe_all(self) -> asyncio.Queue[Event]: ...

    async def unsubscribe_all(self, queue: asyncio.Queue[Event]) -> None: ...

    async def list_events(self, job_id: str, after: str | None = None, limit: int = 20) -> list[Event]: ...

    async def get_event(self, event_id: str) -> Event | None: ...
