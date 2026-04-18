from __future__ import annotations
import asyncio
import uuid
from datetime import datetime
from collections import deque
from typing import Dict, Deque, Set, Callable, Any, AsyncGenerator

from app.schemas.job import EventDTO


class EventType:
    AGENT_START = "agent_start"
    AGENT_STEP = "agent_step"
    AGENT_END = "agent_end"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    FILE_WRITE = "file_write"
    LOG = "log"
    ERROR = "error"
    STATUS_CHANGE = "status_change"


class EventBus:
    _instance: "EventBus | None" = None
    
    def __init__(self):
        self._job_events: Dict[str, Deque[EventDTO]] = {}
        self._subscribers: Dict[str, Set[asyncio.Queue[EventDTO]]] = {}
        self._max_history: int = 1000
        self._lock = asyncio.Lock()
    
    @classmethod
    def get_instance(cls) -> "EventBus":
        if cls._instance is None:
            cls._instance = EventBus()
        return cls._instance
    
    async def publish(self, job_id: str, event_type: str, payload: Dict[str, Any], 
                     step_id: str | None = None, agent_id: str | None = None) -> EventDTO:
        event = EventDTO(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            job_id=job_id,
            step_id=step_id,
            type=event_type,
            agent_id=agent_id,
            payload=payload,
            timestamp=datetime.now()
        )
        
        async with self._lock:
            if job_id not in self._job_events:
                self._job_events[job_id] = deque(maxlen=self._max_history)
            self._job_events[job_id].append(event)
            
            if job_id in self._subscribers:
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
        return queue
    
    async def unsubscribe(self, job_id: str, queue: asyncio.Queue[EventDTO]) -> None:
        async with self._lock:
            if job_id in self._subscribers:
                self._subscribers[job_id].discard(queue)
                if not self._subscribers[job_id]:
                    del self._subscribers[job_id]
    
    async def list_events(self, job_id: str, after: str | None = None, limit: int = 100) -> list[EventDTO]:
        async with self._lock:
            events = list(self._job_events.get(job_id, []))
        
        if after:
            for i, event in enumerate(events):
                if event.event_id == after:
                    events = events[i+1:]
                    break
        
        return events[-limit:]
