from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Deque, Dict, Set

from app.abstractions.job_event_bus import (
    DurableEventListener,
    EventSubscriberOverflowError,
    EventSubscriptionProtocol,
)
from app.core.identifier import create_prefixed_id
from app.schemas.event import (
    Event,
    BaseEvent,
    MessageCreatedEvent, MessageCreatedPayload,
    JobCreatedEvent, JobCreatedPayload,
    JobStartedEvent, JobStartedPayload,
    JobCompletedEvent, JobCompletedPayload,
    JobCancelledEvent, JobCancelledPayload,
    JobFailedEvent, JobFailedPayload,
    StatusChangeEvent, StatusChangePayload,
    AgentStartEvent, AgentStartPayload,
    AgentStepEvent, AgentStepPayload,
    AgentEndEvent, AgentEndPayload,
    ToolCallStartEvent, ToolCallStartPayload,
    ToolCallEndEvent, ToolCallEndPayload,
    ErrorEvent, ErrorPayload,
    LLMRequestEvent, LLMRequestPayload,
    TextStartEvent, TextStartPayload,
    TextDeltaEvent, TextDeltaPayload,
    TextEndEvent, TextEndPayload,
    SessionInterruptedEvent, SessionInterruptedPayload,
)

logger = logging.getLogger(__name__)


class EventSubscription(asyncio.Queue[Event]):
    """支持事件类型过滤，并在溢出后向消费者暴露明确错误的临时订阅。"""

    def __init__(
        self,
        *,
        job_id: str,
        subscriber_kind: str,
        metadata: Mapping[str, str] | None,
        maxsize: int,
        event_types: frozenset[str] | None,
    ) -> None:
        super().__init__(maxsize=maxsize)
        self.job_id = job_id
        self.subscription_id = create_prefixed_id("sub")
        self.subscriber_kind = subscriber_kind
        self.metadata = MappingProxyType(dict(metadata or {}))
        self.created_at = datetime.now().astimezone()
        self.event_types = event_types
        self._overflow_error: EventSubscriberOverflowError | None = None

    def accepts(self, event_type: str) -> bool:
        return self.event_types is None or event_type in self.event_types

    def offer(self, event: Event) -> bool:
        if not self.accepts(event.type):
            return True
        try:
            self.put_nowait(event)
        except asyncio.QueueFull:
            self._overflow_error = EventSubscriberOverflowError(
                subscription_id=self.subscription_id,
                subscriber_kind=self.subscriber_kind,
                job_id=event.job_id,
                event_type=event.type,
                max_queue_size=self.maxsize,
            )
            return False
        return True

    async def get(self) -> Event:
        if self._overflow_error is not None:
            raise self._overflow_error
        event = await super().get()
        if self._overflow_error is not None:
            raise self._overflow_error
        return event

    @property
    def overflow_error(self) -> EventSubscriberOverflowError | None:
        return self._overflow_error


class EventType:
    """事件类型常量"""
    # Job 生命周期
    JOB_CREATED = "job_created"
    JOB_STARTED = "job_started"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"

    # Agent 执行
    AGENT_START = "agent_start"
    AGENT_STEP = "agent_step"
    AGENT_END = "agent_end"

    # LLM 调用
    LLM_REQUEST = "llm_request"

    # 流式文本输出
    TEXT_START = "text_start"
    TEXT_DELTA = "text_delta"
    TEXT_END = "text_end"

    # 工具调用
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"

    # 消息事件
    MESSAGE_CREATED = "message_created"

    # 错误与状态
    ERROR = "error"
    STATUS_CHANGE = "status_change"

    # Session 打断
    SESSION_INTERRUPTED = "session_interrupted"


@dataclass(frozen=True)
class EventFactorySpec:
    event_type: str
    event_class: type[BaseEvent]
    payload_class: type[Any]

    def build(
        self,
        *,
        job_id: str,
        payload: Dict[str, Any],
        step_id: str | None,
        agent_id: str | None,
    ) -> Event:
        event_payload = dict(payload)
        part_id = event_payload.pop("part_id", None)
        return self.event_class(
            event_id=create_prefixed_id("evt"),
            part_id=part_id,
            job_id=job_id,
            step_id=step_id,
            agent_id=agent_id,
            timestamp=datetime.now(timezone.utc),
            type=self.event_type,
            payload=self.payload_class(**event_payload),
        )


EVENT_FACTORY_REGISTRY: dict[str, EventFactorySpec] = {
    EventType.MESSAGE_CREATED: EventFactorySpec(EventType.MESSAGE_CREATED, MessageCreatedEvent, MessageCreatedPayload),
    EventType.JOB_CREATED: EventFactorySpec(EventType.JOB_CREATED, JobCreatedEvent, JobCreatedPayload),
    EventType.JOB_STARTED: EventFactorySpec(EventType.JOB_STARTED, JobStartedEvent, JobStartedPayload),
    EventType.JOB_COMPLETED: EventFactorySpec(EventType.JOB_COMPLETED, JobCompletedEvent, JobCompletedPayload),
    EventType.JOB_CANCELLED: EventFactorySpec(EventType.JOB_CANCELLED, JobCancelledEvent, JobCancelledPayload),
    EventType.JOB_FAILED: EventFactorySpec(EventType.JOB_FAILED, JobFailedEvent, JobFailedPayload),
    EventType.STATUS_CHANGE: EventFactorySpec(EventType.STATUS_CHANGE, StatusChangeEvent, StatusChangePayload),
    EventType.LLM_REQUEST: EventFactorySpec(EventType.LLM_REQUEST, LLMRequestEvent, LLMRequestPayload),
    EventType.TEXT_START: EventFactorySpec(EventType.TEXT_START, TextStartEvent, TextStartPayload),
    EventType.TEXT_DELTA: EventFactorySpec(EventType.TEXT_DELTA, TextDeltaEvent, TextDeltaPayload),
    EventType.TEXT_END: EventFactorySpec(EventType.TEXT_END, TextEndEvent, TextEndPayload),
    EventType.AGENT_START: EventFactorySpec(EventType.AGENT_START, AgentStartEvent, AgentStartPayload),
    EventType.AGENT_STEP: EventFactorySpec(EventType.AGENT_STEP, AgentStepEvent, AgentStepPayload),
    EventType.AGENT_END: EventFactorySpec(EventType.AGENT_END, AgentEndEvent, AgentEndPayload),
    EventType.TOOL_CALL_START: EventFactorySpec(EventType.TOOL_CALL_START, ToolCallStartEvent, ToolCallStartPayload),
    EventType.TOOL_CALL_END: EventFactorySpec(EventType.TOOL_CALL_END, ToolCallEndEvent, ToolCallEndPayload),
    EventType.ERROR: EventFactorySpec(EventType.ERROR, ErrorEvent, ErrorPayload),
    EventType.SESSION_INTERRUPTED: EventFactorySpec(EventType.SESSION_INTERRUPTED, SessionInterruptedEvent, SessionInterruptedPayload),
}


class JobEventBus:
    def __init__(self):
        self._job_events: Dict[str, Deque[Event]] = {}
        self._subscribers: Dict[str, Set[EventSubscription]] = {}
        self._durable_listeners: Set[DurableEventListener] = set()
        self._max_history: int = 1000
        self._lock = asyncio.Lock()
        self._job_publish_locks: Dict[str, asyncio.Lock] = {}

    async def publish(
        self,
        job_id: str,
        event_type: str,
        payload: Dict[str, Any],
        step_id: str | None = None,
        agent_id: str | None = None,
    ) -> Event:
        """
        发布事件到总线。

        内部使用新的事件类型（discriminated union），
        但对外接口保持不变（接收 event_type 字符串和 payload 字典）。
        """

        # 根据 event_type 构建具体的事件对象
        event = self._build_event(job_id, event_type, payload, step_id, agent_id)
        async with self._lock:
            publish_lock = self._job_publish_locks.setdefault(job_id, asyncio.Lock())

        async with publish_lock:
            # 持久化监听器属于发布事务的一部分。写入失败时 publish 直接失败，
            # 不允许临时 SSE 消费者先看到一个没有被权威存储记录的事件。
            async with self._lock:
                durable_listeners = tuple(self._durable_listeners)

            for listener in durable_listeners:
                await listener(event)

            # 持久化成功后再更新内存历史并广播给临时订阅者。
            async with self._lock:
                if job_id not in self._job_events:
                    self._job_events[job_id] = deque(maxlen=self._max_history)
                self._job_events[job_id].append(event)

                subscribers = self._subscribers.get(job_id)
                if subscribers:
                    overflowed = [subscription for subscription in subscribers if not subscription.offer(event)]
                    for subscription in overflowed:
                        subscribers.remove(subscription)
                        logger.error(
                            "%s metadata=%s created_at=%s",
                            subscription.overflow_error,
                            dict(subscription.metadata),
                            subscription.created_at.isoformat(),
                        )
                    if not subscribers:
                        del self._subscribers[job_id]

        return event

    def _build_event(
        self,
        job_id: str,
        event_type: str,
        payload: Dict[str, Any],
        step_id: str | None,
        agent_id: str | None,
    ) -> Event:
        """根据事件注册表构建对应的新事件对象。"""
        factory_spec = EVENT_FACTORY_REGISTRY.get(event_type)
        if factory_spec is None:
            raise ValueError(f"Unknown event type: {event_type}. Please add a new EventFactorySpec.")
        return factory_spec.build(
            job_id=job_id,
            payload=payload,
            step_id=step_id,
            agent_id=agent_id,
        )

    async def subscribe(
        self,
        job_id: str,
        *,
        subscriber_kind: str,
        metadata: Mapping[str, str] | None = None,
        event_types: frozenset[str] | None = None,
    ) -> EventSubscription:
        if not subscriber_kind:
            raise ValueError("subscriber_kind 不能为空")
        subscription = EventSubscription(
            job_id=job_id,
            subscriber_kind=subscriber_kind,
            metadata=metadata,
            maxsize=100,
            event_types=event_types,
        )
        async with self._lock:
            if job_id not in self._subscribers:
                self._subscribers[job_id] = set()
            self._subscribers[job_id].add(subscription)
        logger.info(
            "事件订阅已创建: subscription_id=%s subscriber_kind=%s job_id=%s "
            "event_types=%s created_at=%s metadata=%s",
            subscription.subscription_id,
            subscription.subscriber_kind,
            job_id,
            sorted(event_types) if event_types else "all",
            subscription.created_at.isoformat(),
            dict(subscription.metadata),
        )
        return subscription

    async def unsubscribe(
        self,
        job_id: str,
        subscription: EventSubscriptionProtocol,
        *,
        reason: str,
    ) -> None:
        async with self._lock:
            removed = False
            if job_id in self._subscribers:
                if subscription in self._subscribers[job_id]:
                    self._subscribers[job_id].remove(subscription)
                    removed = True
                if not self._subscribers[job_id]:
                    del self._subscribers[job_id]
        logger.info(
            "事件订阅已解除: subscription_id=%s subscriber_kind=%s job_id=%s "
            "reason=%s removed=%s metadata=%s",
            subscription.subscription_id,
            subscription.subscriber_kind,
            job_id,
            reason,
            removed,
            dict(subscription.metadata),
        )

    async def register_durable_listener(self, listener: DurableEventListener) -> None:
        async with self._lock:
            self._durable_listeners.add(listener)

    async def unregister_durable_listener(self, listener: DurableEventListener) -> None:
        async with self._lock:
            self._durable_listeners.discard(listener)

    async def list_events(self, job_id: str, after: str | None = None, limit: int = 20) -> list[Event]:
        """获取事件列表（返回 discriminated union 类型）"""
        async with self._lock:
            events = list(self._job_events.get(job_id, []))

        if after:
            for index, event in enumerate(events):
                if event.event_id == after:
                    events = events[index + 1 :]
                    break

        return events[-limit:]

    async def get_event(self, event_id: str) -> Event | None:
        """按事件 ID 查询单个事件。"""
        async with self._lock:
            event_groups = [list(events) for events in self._job_events.values()]

        for events in event_groups:
            for event in events:
                if event.event_id == event_id:
                    return event
        return None
