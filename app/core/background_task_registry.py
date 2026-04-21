from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional


@dataclass(slots=True)
class BackgroundTaskHandle:
    task_id: str
    session_id: str
    task_name: str
    status: str
    created_at: datetime
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "task_name": self.task_name,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class _BackgroundTaskRecord:
    handle: BackgroundTaskHandle
    task: asyncio.Task[Any]


class BackgroundTaskRegistry:
    _instance: "BackgroundTaskRegistry | None" = None

    def __init__(self):
        self._tasks: dict[str, dict[str, _BackgroundTaskRecord]] = {}

    @classmethod
    def get_instance(cls) -> "BackgroundTaskRegistry":
        if cls._instance is None:
            cls._instance = BackgroundTaskRegistry()
        return cls._instance

    def spawn(
        self,
        session_id: str,
        task_name: str,
        runner: Callable[[], Awaitable[Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> BackgroundTaskHandle:
        task_id = f"bgt_{uuid.uuid4().hex[:12]}"
        handle = BackgroundTaskHandle(
            task_id=task_id,
            session_id=session_id,
            task_name=task_name,
            status="pending",
            created_at=datetime.now(),
            metadata=metadata or {},
        )

        async def _wrapped_runner() -> Any:
            handle.status = "running"
            handle.started_at = datetime.now()

            try:
                result = await runner()
                handle.status = "completed"
                return result
            except asyncio.CancelledError:
                handle.status = "cancelled"
                raise
            except Exception:
                handle.status = "failed"
                raise
            finally:
                handle.ended_at = datetime.now()

        task = asyncio.create_task(
            _wrapped_runner(),
            name=f"{task_name}:{session_id}:{task_id}",
        )

        if session_id not in self._tasks:
            self._tasks[session_id] = {}
        self._tasks[session_id][task_id] = _BackgroundTaskRecord(handle=handle, task=task)
        return handle

    def get_handle(self, session_id: str, task_id: str) -> BackgroundTaskHandle | None:
        record = self._tasks.get(session_id, {}).get(task_id)
        return record.handle if record else None

    def get_task(self, session_id: str, task_id: str) -> asyncio.Task[Any] | None:
        record = self._tasks.get(session_id, {}).get(task_id)
        return record.task if record else None

    def list_handles(self, session_id: str) -> list[BackgroundTaskHandle]:
        return [record.handle for record in self._tasks.get(session_id, {}).values()]
