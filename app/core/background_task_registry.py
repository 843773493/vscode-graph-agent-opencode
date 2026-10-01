from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

from app.core.identifier import create_prefixed_id


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

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BackgroundTaskHandle":
        return cls(
            task_id=str(value["task_id"]),
            session_id=str(value["session_id"]),
            task_name=str(value["task_name"]),
            status=str(value["status"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            started_at=(
                datetime.fromisoformat(str(value["started_at"]))
                if value.get("started_at")
                else None
            ),
            ended_at=(
                datetime.fromisoformat(str(value["ended_at"]))
                if value.get("ended_at")
                else None
            ),
            metadata=dict(value.get("metadata") or {}),
        )


class BackgroundTaskHistoryStoreProtocol(Protocol):
    def upsert(self, handle: BackgroundTaskHandle) -> None: ...

    def list_session(self, session_id: str) -> list[BackgroundTaskHandle]: ...

    def mark_active_tasks_lost(self) -> None: ...

    def delete_session(self, session_id: str) -> None: ...


@dataclass(slots=True)
class _BackgroundTaskRecord:
    handle: BackgroundTaskHandle
    task: asyncio.Task[Any]


class BackgroundTaskRegistry:
    def __init__(self, *, history_store: BackgroundTaskHistoryStoreProtocol):
        self._tasks: dict[str, dict[str, _BackgroundTaskRecord]] = {}
        self._history_store = history_store
        self._history_store.mark_active_tasks_lost()

    def spawn(
        self,
        session_id: str,
        task_name: str,
        runner: Callable[[], Awaitable[Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> BackgroundTaskHandle:
        task_id = create_prefixed_id("bgt")
        handle = BackgroundTaskHandle(
            task_id=task_id,
            session_id=session_id,
            task_name=task_name,
            status="running",
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            metadata=metadata or {},
        )
        self._history_store.upsert(handle)

        async def _wrapped_runner() -> Any:
            try:
                result = await runner()
                handle.status = "completed"
                if result is not None:
                    handle.metadata["result"] = result
                return result
            except asyncio.CancelledError:
                handle.status = "cancelled"
                raise
            except Exception as exc:
                handle.status = "failed"
                handle.metadata["error_message"] = str(exc)
                raise
            finally:
                handle.ended_at = datetime.now(timezone.utc)
                self._history_store.upsert(handle)

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
        return [
            record.handle
            for record in self._tasks.get(session_id, {}).values()
            if record.handle.status in {"pending", "running"}
        ]

    def list_closed_handles(self, session_id: str) -> list[BackgroundTaskHandle]:
        return [
            handle
            for handle in self._history_store.list_session(session_id)
            if handle.status not in {"pending", "running"}
        ]

    def list_active_handles(self) -> list[BackgroundTaskHandle]:
        return [
            record.handle
            for session_tasks in self._tasks.values()
            for record in session_tasks.values()
            if record.handle.status in {"pending", "running"}
        ]

    async def cancel_all_active(self, *, reason: str) -> int:
        active = [
            (handle.session_id, handle.task_id)
            for handle in self.list_active_handles()
        ]
        for session_id, task_id in active:
            handle = await self.cancel(session_id, task_id)
            handle.metadata["cancel_reason"] = reason
            self._history_store.upsert(handle)
        return len(active)

    async def cancel(self, session_id: str, task_id: str) -> BackgroundTaskHandle:
        record = self._tasks.get(session_id, {}).get(task_id)
        if record is None:
            raise ValueError(f"后台任务不存在: session_id={session_id}, task_id={task_id}")

        if record.task.done():
            return record.handle

        record.task.cancel()
        try:
            await record.task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            raise RuntimeError(f"取消后台任务失败: task_id={task_id}, error={exc}") from exc
        if record.handle.status in {"pending", "running"}:
            record.handle.status = "cancelled"
            if record.handle.ended_at is None:
                record.handle.ended_at = datetime.now(timezone.utc)
        self._history_store.upsert(record.handle)
        return record.handle

    async def delete(self, session_id: str, task_id: str) -> BackgroundTaskHandle:
        record = self._tasks.get(session_id, {}).get(task_id)
        if record is None:
            historical_handle = next(
                (
                    handle
                    for handle in self._history_store.list_session(session_id)
                    if handle.task_id == task_id
                ),
                None,
            )
            if historical_handle is None:
                raise ValueError(
                    f"后台任务不存在: session_id={session_id}, task_id={task_id}"
                )
            historical_handle.status = "deleted"
            if historical_handle.ended_at is None:
                historical_handle.ended_at = datetime.now(timezone.utc)
            self._history_store.upsert(historical_handle)
            return historical_handle

        if not record.task.done():
            await self.cancel(session_id, task_id)

        record.handle.status = "deleted"
        if record.handle.ended_at is None:
            record.handle.ended_at = datetime.now(timezone.utc)
        self._history_store.upsert(record.handle)

        del self._tasks[session_id][task_id]
        if not self._tasks[session_id]:
            del self._tasks[session_id]
        return record.handle

    async def delete_session(self, session_id: str) -> int:
        task_ids = list(self._tasks.get(session_id, {}).keys())
        for task_id in task_ids:
            await self.delete(session_id, task_id)
        self._history_store.delete_session(session_id)
        return len(task_ids)
