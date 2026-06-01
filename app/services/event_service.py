from __future__ import annotations
import asyncio
from typing import AsyncGenerator

from app.schemas.event import Event
from app.core.job_event_bus import JobEventBus


class EventService:
    def __init__(self, *, bus: JobEventBus):
        self.bus = bus
    
    async def list(self, job_id: str, after: str | None = None, limit: int = 100) -> list[Event]:
        """获取事件列表"""
        if self.bus is None:
            raise RuntimeError("EventService 未绑定 JobEventBus")
        return await self.bus.list_events(job_id, after, limit)
    
    async def list_by_job(self, job_id: str) -> list[Event]:
        """获取某个job的所有事件"""
        if self.bus is None:
            raise RuntimeError("EventService 未绑定 JobEventBus")
        return await self.bus.list_events(job_id)
    
    async def get(self, event_id: str) -> Event | None:
        """根据event_id获取单个事件"""
        if self.bus is None:
            raise RuntimeError("EventService 未绑定 JobEventBus")
        for job_events in self.bus._job_events.values():
            for event in job_events:
                if event.event_id == event_id:
                    return event
        return None

    async def stream_sse(self, job_id: str) -> AsyncGenerator[str, None]:
        """
        SSE流式推送事件。
        
        返回的数据格式：
        event: {event_type}
        data: {event_json}
        """
        if self.bus is None:
            raise RuntimeError("EventService 未绑定 JobEventBus")
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
