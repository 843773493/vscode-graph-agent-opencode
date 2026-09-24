from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Mapping

from app.abstractions.job_event_bus import (
    EventSubscriberOverflowError,
    JobEventBusProtocol,
)
from app.core.job_event_bus import JOB_EVENT_HISTORY_SIZE
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

    def _require_bus(self) -> JobEventBusProtocol:
        if self.bus is None:
            raise RuntimeError("EventService 未绑定 JobEventBus")
        return self.bus

    def _sse_block(self, event: Event) -> str:
        """把一条内部事件编码成 SSE 数据块。"""
        observation = map_event_to_observation_proto(event)
        data = json.dumps(
            session_sse_to_json(observation),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            f"id: {observation.event.header.event_id}\n"
            f"event: {observation.event.type}\n"
            f"data: {data}\n\n"
        )

    async def _events_after_cursor(self, job_id: str, event_id: str) -> list[Event]:
        """返回保留窗口内该游标之后的事件；游标已失效时显式报错。

        ``list_events(after=...)`` 在历史里找不到游标时会退化为返回整段保留
        窗口，直接用它重放会把整段历史当成「游标之后的新事件」重复投递。这里
        改为显式定位游标，落在窗口之外即视为失效。
        """
        retained_events = await self._require_bus().list_events(
            job_id, limit=JOB_EVENT_HISTORY_SIZE
        )
        for index, event in enumerate(retained_events):
            if event.event_id == event_id:
                return retained_events[index + 1 :]
        raise JobEventCursorGoneError(job_id=job_id, event_id=event_id)

    async def list(
        self,
        job_id: str,
        after: str | None = None,
        limit: int = 100,
    ) -> list[Event]:
        """获取事件列表"""
        return await self._require_bus().list_events(job_id, after, limit)

    async def list_by_job(self, job_id: str) -> list[Event]:
        """获取某个job的所有事件（保留窗口内全部，不套用 list_events 的默认 20 条）"""
        return await self._require_bus().list_events(
            job_id, limit=JOB_EVENT_HISTORY_SIZE
        )

    async def get(self, event_id: str) -> Event | None:
        """根据event_id获取单个事件"""
        return await self._require_bus().get_event(event_id)

    async def ensure_cursor(self, job_id: str, event_id: str | None) -> None:
        if not event_id:
            return
        await self._events_after_cursor(job_id, event_id)

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
        bus = self._require_bus()
        subscription = await bus.subscribe(
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
                for event in await self._events_after_cursor(job_id, after_event_id):
                    replayed_event_ids.add(event.event_id)
                    yield self._sse_block(event)
            while True:
                try:
                    event = await asyncio.wait_for(subscription.get(), timeout=30)
                    if event.event_id in replayed_event_ids:
                        replayed_event_ids.remove(event.event_id)
                        continue
                    yield self._sse_block(event)
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
            await bus.unsubscribe(
                job_id,
                subscription,
                reason="sse_stream_closed",
            )
