from __future__ import annotations
import asyncio
from typing import AsyncGenerator

from app.schemas.job import EventDTO
from app.core.job_event_bus import JobEventBus


class EventService:
    def __init__(self):
        self.bus = JobEventBus.get_instance()
    
    async def list(self, job_id: str, after: str | None = None, limit: int = 100) -> list[EventDTO]:
        return await self.bus.list_events(job_id, after, limit)
    
    async def list_by_job(self, job_id: str) -> list[EventDTO]:
        return await self.bus.list_events(job_id)
    
    async def get(self, event_id: str) -> EventDTO | None:
        for job_events in self.bus._job_events.values():
            for event in job_events:
                if event.event_id == event_id:
                    return event
        return None
    
    async def stream_sse(self, job_id: str) -> AsyncGenerator[str, None]:
        queue = await self.bus.subscribe(job_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"event: {event.type}\n"
                    yield f"data: {event.model_dump_json()}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            await self.bus.unsubscribe(job_id, queue)
