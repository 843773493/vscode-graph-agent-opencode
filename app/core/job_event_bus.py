from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Deque, Dict, Set

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
        return self.event_class(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            job_id=job_id,
            step_id=step_id,
            agent_id=agent_id,
            timestamp=datetime.now(),
            type=self.event_type,
            payload=self.payload_class(**payload),
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
        self._subscribers: Dict[str, Set[asyncio.Queue[Event]]] = {}
        self._global_subscribers: Set[asyncio.Queue[Event]] = set()
        self._max_history: int = 1000
        self._lock = asyncio.Lock()
        self._listener_count: int = 0

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
        import logging
        logger = logging.getLogger(__name__)
        logger.info("[job_event_bus] publish: job_id=%s event_type=%s listener_count=%s payload_keys=%s", job_id, event_type, self._listener_count, list(payload.keys()))

        # 存储并广播
        async with self._lock:
            if job_id not in self._job_events:
                self._job_events[job_id] = deque(maxlen=self._max_history)
            self._job_events[job_id].append(event)

            if self._listener_count > 0 and job_id in self._subscribers:
                for queue in list(self._subscribers[job_id]):
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass

            if self._listener_count > 0 and self._global_subscribers:
                for queue in list(self._global_subscribers):
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass

        logger.info("[job_event_bus] publish done: job_id=%s event_type=%s event_id=%s", job_id, event_type, event.event_id)

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

    async def subscribe(self, job_id: str) -> asyncio.Queue[Event]:
        queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            if job_id not in self._subscribers:
                self._subscribers[job_id] = set()
            self._subscribers[job_id].add(queue)
            self._listener_count += 1
        return queue

    async def unsubscribe(self, job_id: str, queue: asyncio.Queue[Event]) -> None:
        async with self._lock:
            if job_id in self._subscribers:
                self._subscribers[job_id].discard(queue)
                self._listener_count -= 1
                if not self._subscribers[job_id]:
                    del self._subscribers[job_id]

    async def subscribe_all(self) -> asyncio.Queue[Event]:
        queue = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._global_subscribers.add(queue)
            self._listener_count += 1
        return queue

    async def unsubscribe_all(self, queue: asyncio.Queue[Event]) -> None:
        async with self._lock:
            self._global_subscribers.discard(queue)
            self._listener_count -= 1

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
