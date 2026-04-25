from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, Set

from app.schemas.event import (
    Event,
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
    ErrorEvent, ErrorPayload,
    LLMRequestEvent, LLMRequestPayload,
    LogEvent,
    ToolCallEvent,
    FileWriteEvent,
    ModelCallEvent,
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

    # 消息事件
    MESSAGE_CREATED = "message_created"

    # 错误与状态
    ERROR = "error"
    STATUS_CHANGE = "status_change"

    # 预留事件（为兼容性保留，当前未广泛使用）
    # TODO: LOG 事件需定义明确的 payload schema（如 LogLevel 枚举、结构化日志字段）
    LOG = "log"
    TOOL_CALL = "tool_call"  # TODO: 工具调用事件需规范化参数
    FILE_WRITE = "file_write"  # TODO: 文件写入事件需补充权限、路径校验
    MODEL_CALL = "model_call"  # TODO: 已废弃，使用 LLM_REQUEST 替代


class JobEventBus:
    _instance: "JobEventBus | None" = None

    def __init__(self):
        self._job_events: Dict[str, Deque[Event]] = {}
        self._subscribers: Dict[str, Set[asyncio.Queue[Event]]] = {}
        self._max_history: int = 1000
        self._lock = asyncio.Lock()
        self._listener_count: int = 0

    @classmethod
    def get_instance(cls) -> "JobEventBus":
        if cls._instance is None:
            cls._instance = JobEventBus()
        return cls._instance

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

        return event

    def _build_event(
        self,
        job_id: str,
        event_type: str,
        payload: Dict[str, Any],
        step_id: str | None,
        agent_id: str | None,
    ) -> Event:
        """根据 event_type 构建对应的新事件对象"""

        # 通用字段
        common = {
            "event_id": f"evt_{uuid.uuid4().hex[:12]}",
            "job_id": job_id,
            "step_id": step_id,
            "agent_id": agent_id,
            "timestamp": datetime.now(),
        }

        # 根据类型构建具体事件
        t = event_type

        if t == EventType.MESSAGE_CREATED:
            return MessageCreatedEvent(
                type="message_created",
                payload=MessageCreatedPayload(**payload),
                **common
            )

        elif t == EventType.JOB_CREATED:
            return JobCreatedEvent(
                type="job_created",
                payload=JobCreatedPayload(**payload),
                **common
            )

        elif t == EventType.JOB_STARTED:
            return JobStartedEvent(
                type="job_started",
                payload=JobStartedPayload(),
                **common
            )

        elif t == EventType.JOB_COMPLETED:
            return JobCompletedEvent(
                type="job_completed",
                payload=JobCompletedPayload(**payload),
                **common
            )

        elif t == EventType.JOB_CANCELLED:
            return JobCancelledEvent(
                type="job_cancelled",
                payload=JobCancelledPayload(),
                **common
            )

        elif t == EventType.JOB_FAILED:
            return JobFailedEvent(
                type="job_failed",
                payload=JobFailedPayload(**payload),
                **common
            )

        elif t == EventType.STATUS_CHANGE:
            return StatusChangeEvent(
                type="status_change",
                payload=StatusChangePayload(**payload),
                **common
            )

        elif t == EventType.AGENT_START:
            return AgentStartEvent(
                type="agent_start",
                payload=AgentStartPayload(**payload),
                **common
            )

        elif t == EventType.AGENT_STEP:
            return AgentStepEvent(
                type="agent_step",
                payload=AgentStepPayload(**payload),
                **common
            )

        elif t == EventType.AGENT_END:
            return AgentEndEvent(
                type="agent_end",
                payload=AgentEndPayload(**payload),
                **common
            )

        elif t == EventType.ERROR:
            return ErrorEvent(
                type="error",
                payload=ErrorPayload(**payload),
                **common
            )

        elif t == EventType.LLM_REQUEST:
            return LLMRequestEvent(
                type="llm_request",
                payload=LLMRequestPayload(**payload),
                **common
            )

        elif t == EventType.LOG:
            # TODO: LOG 事件为预留类型，payload 使用原始 dict，需定义 LogLevel 枚举和结构化字段
            return LogEvent(
                type="log",
                payload=payload,  # dict directly
                **common
            )

        elif t == EventType.TOOL_CALL:
            # TODO: TOOL_CALL 事件需规范化 payload 结构（tool_id、parameters、result 等字段）
            return ToolCallEvent(
                type="tool_call",
                payload=payload,
                **common
            )

        elif t == EventType.FILE_WRITE:
            # TODO: FILE_WRITE 事件需补充文件路径、权限、操作类型等结构化字段
            return FileWriteEvent(
                type="file_write",
                payload=payload,
                **common
            )

        elif t == EventType.MODEL_CALL:
            # TODO: MODEL_CALL 已废弃，应使用 LLM_REQUEST 替代。保留仅为了向后兼容。
            return ModelCallEvent(
                type="model_call",
                payload=payload,
                **common
            )

        elif t == EventType.TOOL_CALL:
            return ToolCallEvent(
                type="tool_call",
                payload=payload,
                **common
            )

        elif t == EventType.FILE_WRITE:
            return FileWriteEvent(
                type="file_write",
                payload=payload,
                **common
            )

        elif t == EventType.MODEL_CALL:
            return ModelCallEvent(
                type="model_call",
                payload=payload,
                **common
            )

        else:
            raise ValueError(f"Unknown event type: {event_type}. Please add a new Event subclass.")

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