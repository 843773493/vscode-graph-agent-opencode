from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, Set

from app.schemas.job import EventDTO


class EventType:
    JOB_CREATED = "job_created"
    JOB_STARTED = "job_started"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"
    AGENT_START = "agent_start"
    AGENT_STEP = "agent_step"
    AGENT_END = "agent_end"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    FILE_WRITE = "file_write"
    LOG = "log"
    ERROR = "error"
    STATUS_CHANGE = "status_change"


class JobEventBus:
    _instance: "JobEventBus | None" = None

    def __init__(self):
        self._job_events: Dict[str, Deque[EventDTO]] = {}
        self._subscribers: Dict[str, Set[asyncio.Queue[EventDTO]]] = {}
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
    ) -> EventDTO:
        event = EventDTO(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            job_id=job_id,
            step_id=step_id,
            type=event_type,
            agent_id=agent_id,
            payload=payload,
            timestamp=datetime.now(),
        )

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

    async def subscribe(self, job_id: str) -> asyncio.Queue[EventDTO]:
        queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            if job_id not in self._subscribers:
                self._subscribers[job_id] = set()
            self._subscribers[job_id].add(queue)
            self._listener_count += 1
        return queue

    async def unsubscribe(self, job_id: str, queue: asyncio.Queue[EventDTO]) -> None:
        async with self._lock:
            if job_id in self._subscribers:
                self._subscribers[job_id].discard(queue)
                self._listener_count -= 1
                if not self._subscribers[job_id]:
                    del self._subscribers[job_id]

    async def list_events(self, job_id: str, after: str | None = None, limit: int = 20) -> list[EventDTO]:
        async with self._lock:
            events = list(self._job_events.get(job_id, []))

        if after:
            for index, event in enumerate(events):
                if event.event_id == after:
                    events = events[index + 1 :]
                    break

        return events[-limit:]