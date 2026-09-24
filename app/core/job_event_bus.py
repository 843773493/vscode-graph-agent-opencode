from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final

from app.abstractions.job_event_bus import (
    DurableEventListener,
    EventSubscriberOverflowError,
    EventSubscriptionProtocol,
)
from app.core.identifier import create_prefixed_id
from app.schemas.event import (
    AgentEndEvent,
    AgentEndPayload,
    AgentStartEvent,
    AgentStartPayload,
    AgentStepEvent,
    AgentStepPayload,
    BaseEvent,
    ErrorEvent,
    ErrorPayload,
    Event,
    GoalClearedEvent,
    GoalClearedPayload,
    GoalUpdatedEvent,
    GoalUpdatedPayload,
    JobCancelledEvent,
    JobCancelledPayload,
    JobCompletedEvent,
    JobCompletedPayload,
    JobCreatedEvent,
    JobCreatedPayload,
    JobFailedEvent,
    JobFailedPayload,
    JobStartedEvent,
    JobStartedPayload,
    LLMRequestEvent,
    LLMRequestPayload,
    MessageCreatedEvent,
    MessageCreatedPayload,
    ModelFailedEvent,
    ModelFailedPayload,
    SessionInterruptedEvent,
    SessionInterruptedPayload,
    StatusChangeEvent,
    StatusChangePayload,
    TextDeltaEvent,
    TextDeltaPayload,
    TextEndEvent,
    TextEndPayload,
    TextStartEvent,
    TextStartPayload,
    ToolCallEndEvent,
    ToolCallEndPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)

# OpenSpec 3.8-B：Job event bus 是通用 EventChannelService 之上的 typed
# adapter；该依赖方向由 OpenSpec 指定（events 基础设施位于
# app/services/infrastructure/events/，channel 队列/溢出/历史语义由它承载）。
from app.services.infrastructure.events.event_channel_service import (
    JOB_EVENTS_CHANNEL_KIND,
    EventChannel,
    EventChannelService,
    EventChannelSpec,
    channel_name,
)

logger = logging.getLogger(__name__)

# 临时订阅队列上限与短期历史长度保持既有行为不变。
JOB_EVENT_QUEUE_SIZE: Final[int] = 100
JOB_EVENT_HISTORY_SIZE: Final[int] = 1000


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
    MODEL_FAILED = "model_failed"

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
    GOAL_UPDATED = "goal_updated"
    GOAL_CLEARED = "goal_cleared"

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
        payload: dict[str, Any],
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
            timestamp=datetime.now(UTC),
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
    EventType.GOAL_UPDATED: EventFactorySpec(EventType.GOAL_UPDATED, GoalUpdatedEvent, GoalUpdatedPayload),
    EventType.GOAL_CLEARED: EventFactorySpec(EventType.GOAL_CLEARED, GoalClearedEvent, GoalClearedPayload),
    EventType.LLM_REQUEST: EventFactorySpec(EventType.LLM_REQUEST, LLMRequestEvent, LLMRequestPayload),
    EventType.MODEL_FAILED: EventFactorySpec(EventType.MODEL_FAILED, ModelFailedEvent, ModelFailedPayload),
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


class _EventSubscriptionSink:
    """把 :class:`EventSubscription` 适配成 channel 的 fail_closed sink 合同。

    溢出时 ``EventSubscription.offer`` 自己记录 :class:`EventSubscriberOverflowError`；
    这里负责把溢出显式写入日志后返回 False，由 channel 把订阅者从后续投递移除。
    """

    __slots__ = ("subscription",)

    def __init__(self, subscription: EventSubscription) -> None:
        self.subscription = subscription

    def offer(self, event: Event, *, sequence: int) -> bool:
        accepted = self.subscription.offer(event)
        if not accepted:
            error = self.subscription.overflow_error
            if error is not None:
                logger.error(
                    "%s metadata=%s created_at=%s",
                    error,
                    dict(self.subscription.metadata),
                    self.subscription.created_at.isoformat(),
                )
        return accepted


class JobEventBus:
    """`job.events/{job_id}` channel 的 typed adapter（OpenSpec 3.8-B）。

    订阅者队列、溢出移除与短期历史由通用 :class:`EventChannelService` 承载；
    本类只保留 Job 域职责：事件工厂（``EVENT_FACTORY_REGISTRY``）、durable
    listener 发布事务顺序（先持久化监听器，后内存历史与临时订阅者广播）和
    per-job publish 锁串行化。durable listener 属于 Job 事件的持久化适配，
    不是通用 channel 合同的一部分；资源事件不得进入本总线。
    """

    def __init__(self, *, event_service: EventChannelService | None = None) -> None:
        self._event_service = event_service or EventChannelService()
        self._durable_listeners: set[DurableEventListener] = set()
        self._lock = asyncio.Lock()
        self._job_publish_locks: dict[str, asyncio.Lock] = {}

    @property
    def event_channel_service(self) -> EventChannelService:
        """暴露进程内 channel 服务，供组合根共享同一条事件基础设施。"""
        return self._event_service

    def _channel_for(self, job_id: str) -> EventChannel[Event]:
        """取得或创建 `job.events/{job_id}` channel（fail_closed + 短期历史）。"""
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id 必须是非空字符串")
        return self._event_service.ensure_channel(
            EventChannelSpec(
                name=channel_name(JOB_EVENTS_CHANNEL_KIND, job_id),
                overflow_policy="fail_closed",
                max_queue_size=JOB_EVENT_QUEUE_SIZE,
                history_size=JOB_EVENT_HISTORY_SIZE,
            )
        )

    async def publish(
        self,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
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
        channel = self._channel_for(job_id)
        async with self._lock:
            publish_lock = self._job_publish_locks.setdefault(job_id, asyncio.Lock())

        async with publish_lock:
            # 持久化监听器属于发布事务的一部分。写入失败时 publish 直接失败，
            # 不允许临时 SSE 消费者先看到一个没有被权威存储记录的事件。
            async with self._lock:
                durable_listeners = tuple(self._durable_listeners)

            for listener in durable_listeners:
                await listener(event)

            # 持久化成功后写短期历史并广播给临时订阅者；「先历史、后订阅者」
            # 的顺序由 channel.publish 保证。channel 操作是同步的，不会与其它
            # 协程交错；溢出的临时订阅者由 channel 按 fail_closed 移除。
            channel.publish(event)

        return event

    def _build_event(
        self,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
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
        channel = self._channel_for(job_id)
        subscription = EventSubscription(
            job_id=job_id,
            subscriber_kind=subscriber_kind,
            metadata=metadata,
            maxsize=JOB_EVENT_QUEUE_SIZE,
            event_types=event_types,
        )
        # 临时订阅以 fail_closed sink 形式接入 job.events/{job_id} channel：
        # 溢出时 sink 记录显式错误并返回 False，channel 负责把订阅者移除。
        channel.subscribe_with_sink(
            _EventSubscriptionSink(subscription),
            label=subscriber_kind,
            subscription_id=subscription.subscription_id,
        )
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
        channel = self._event_service.find_channel(
            channel_name(JOB_EVENTS_CHANNEL_KIND, job_id)
        )
        removed = False
        if channel is not None:
            removed = channel.unsubscribe(subscription.subscription_id)
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
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError(f"list_events limit 必须是正整数: {limit!r}")
        channel = self._event_service.find_channel(
            channel_name(JOB_EVENTS_CHANNEL_KIND, job_id)
        )
        events = list(channel.history) if channel is not None else []

        if after:
            for index, event in enumerate(events):
                if event.event_id == after:
                    events = events[index + 1 :]
                    break

        return events[-limit:]

    async def get_event(self, event_id: str) -> Event | None:
        """按事件 ID 查询单个事件。"""
        for channel in self._event_service.channels:
            for event in channel.history:
                if event.event_id == event_id:
                    return event
        return None
