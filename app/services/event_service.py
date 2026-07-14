from __future__ import annotations
import asyncio
import logging
from collections.abc import Mapping
from typing import AsyncGenerator

from app.abstractions.job_event_bus import EventSubscriberOverflowError, JobEventBusProtocol
from app.schemas.event import Event
from app.services.mapping.observation_event_mapper import map_event_to_observation_sse


logger = logging.getLogger(__name__)


class EventService:
    def __init__(self, *, bus: JobEventBusProtocol):
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
        return await self.bus.get_event(event_id)

    async def stream_sse(
        self,
        job_id: str,
        *,
        subscriber_metadata: Mapping[str, str] | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        SSE流式推送事件。
        
        返回的数据格式：
        event: {event_type}
        data: {event_json}
        """
        if self.bus is None:
            raise RuntimeError("EventService 未绑定 JobEventBus")
        subscription = await self.bus.subscribe(
            job_id,
            subscriber_kind="job_sse",
            metadata=subscriber_metadata,
        )
        logger.info(
            "Job SSE 已连接: subscription_id=%s job_id=%s metadata=%s",
            subscription.subscription_id,
            job_id,
            dict(subscription.metadata),
        )
        try:
            while True:
                try:
                    event = await asyncio.wait_for(subscription.get(), timeout=30)
                    observation = map_event_to_observation_sse(event)
                    yield f"event: {observation.event.type}\n"
                    yield f"data: {observation.model_dump_json()}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except EventSubscriberOverflowError:
            logger.exception(
                "Job SSE 因订阅溢出关闭: subscription_id=%s job_id=%s metadata=%s",
                subscription.subscription_id,
                job_id,
                dict(subscription.metadata),
            )
            raise
        finally:
            logger.info(
                "Job SSE 已断开: subscription_id=%s job_id=%s metadata=%s",
                subscription.subscription_id,
                job_id,
                dict(subscription.metadata),
            )
            await self.bus.unsubscribe(
                job_id,
                subscription,
                reason="sse_stream_closed",
            )
