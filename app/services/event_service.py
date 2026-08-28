from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Mapping

from app.abstractions.job_event_bus import (
    EventSubscriberOverflowError,
    JobEventBusProtocol,
)
from app.protocol.codecs.session_sse import session_sse_to_json
from app.schemas.event import Event
from app.services.mapping.observation_event_mapper import map_event_to_observation_proto

logger = logging.getLogger(__name__)


class JobEventCursorGoneError(RuntimeError):
    def __init__(self, *, job_id: str, event_id: str) -> None:
        self.job_id = job_id
        self.event_id = event_id
        super().__init__(
            f"Job 事件游标不存在或已失效: job_id={job_id} event_id={event_id}"
        )


class EventService:
    def __init__(self, *, bus: JobEventBusProtocol):
        self.bus = bus

    async def list(
        self,
        job_id: str,
        after: str | None = None,
        limit: int = 100,
    ) -> list[Event]:
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

    async def ensure_cursor(self, job_id: str, event_id: str | None) -> None:
        if not event_id:
            return
        retained_events = await self.bus.list_events(job_id, limit=1000)
        if not any(event.event_id == event_id for event in retained_events):
            raise JobEventCursorGoneError(job_id=job_id, event_id=event_id)

    async def stream_sse(
        self,
        job_id: str,
        *,
        after_event_id: str | None = None,
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
            replayed_event_ids: set[str] = set()
            if after_event_id:
                replay_events = await self.bus.list_events(
                    job_id,
                    after=after_event_id,
                    limit=1000,
                )
                for event in replay_events:
                    replayed_event_ids.add(event.event_id)
                    observation = map_event_to_observation_proto(event)
                    yield (
                        f"id: {observation.event.header.event_id}\n"
                        f"event: {observation.event.type}\n"
                        f"data: {json.dumps(session_sse_to_json(observation), ensure_ascii=False, separators=(',', ':'))}\n\n"
                    )
            while True:
                try:
                    event = await asyncio.wait_for(subscription.get(), timeout=30)
                    if event.event_id in replayed_event_ids:
                        replayed_event_ids.remove(event.event_id)
                        continue
                    observation = map_event_to_observation_proto(event)
                    yield (
                        f"id: {observation.event.header.event_id}\n"
                        f"event: {observation.event.type}\n"
                        f"data: {json.dumps(session_sse_to_json(observation), ensure_ascii=False, separators=(',', ':'))}\n\n"
                    )
                except TimeoutError:
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
