"""Workspace 状态库中工作区活动事件族的唯一实现。

本模块承载 ``workspace_activity`` 这一条垂直链路的全部实现：

- ``workspace_activity`` 行投影（``WorkspaceActivityRecord``）、追加去重语义
  （``ON CONFLICT(event_id) DO NOTHING`` 后回读既有行）、分页读取、保留窗口
  边界计算与按 occurred_at 裁剪；
- 游标失效判定与 ``WorkspaceActivityCursorGoneError``；
- 活动事件的实时订阅与重放服务 ``WorkspaceActivityService``。

``WorkspaceActivityMixin`` 由
``app.services.infrastructure.workspace_state_store.WorkspaceStateStore`` 继承
装配；本模块只依赖宿主类提供的 ``_database``（``SQLiteStateDatabase``），不感知
配置来源、配置 apply 账本或配置事件 outbox 等其它族。

``WorkspaceActivityService`` 需要构造宿主状态库，为避免宿主模块与本模块的导入
环，在实例化时局部导入 ``WorkspaceStateStore``；公开构造签名不变。
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.sqlite_state import SQLiteDiagnostics, utc_now_text

__all__ = [
    "WorkspaceActivityCursorGoneError",
    "WorkspaceActivityMixin",
    "WorkspaceActivityRecord",
    "WorkspaceActivityService",
]


# workspace_activity 行投影：一条工作区级活动事件通知（会话、状态、摘要与
# 发生时间）。event_seq 是 SQLite AUTOINCREMENT 分配的单调游标。
@dataclass(frozen=True, slots=True)
class WorkspaceActivityRecord:
    event_seq: int
    event_id: str
    session_id: str
    status: str
    summary: str
    occurred_at: str


class WorkspaceActivityCursorGoneError(RuntimeError):
    """请求的工作区活动事件游标已不在保留窗口内，调用方必须重新同步。"""


class WorkspaceActivityMixin:
    """WorkspaceStateStore 的工作区活动事件方法族。

    依赖宿主类提供 ``_database``（``SQLiteStateDatabase``）。
    """

    def append_activity(
        self,
        *,
        event_id: str,
        session_id: str,
        status: str,
        summary: str,
        occurred_at: str | None = None,
    ) -> WorkspaceActivityRecord:
        connection = self._database.connection()
        try:
            timestamp = occurred_at or utc_now_text()
            cursor = connection.execute(
                """
                INSERT INTO workspace_activity(
                    event_id, session_id, status, summary, occurred_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (event_id, session_id, status, summary, timestamp),
            )
            if cursor.rowcount == 0:
                existing = connection.execute(
                    """
                    SELECT event_seq, event_id, session_id, status, summary, occurred_at
                    FROM workspace_activity
                    WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if existing is None:
                    raise RuntimeError(f"Workspace 活动事件去重后无法读取: {event_id}")
                return _activity_record_from_row(existing)
            return WorkspaceActivityRecord(
                event_seq=int(cursor.lastrowid),
                event_id=event_id,
                session_id=session_id,
                status=status,
                summary=summary,
                occurred_at=timestamp,
            )
        finally:
            connection.close()

    def list_activity(
        self,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[WorkspaceActivityRecord, ...]:
        if after < 0 or limit < 1 or limit > 2000:
            raise ValueError("Workspace 活动事件分页参数无效")
        connection = self._database.connection()
        try:
            rows = connection.execute(
                """
                SELECT event_seq, event_id, session_id, status, summary, occurred_at
                FROM workspace_activity
                WHERE event_seq > ?
                ORDER BY event_seq ASC
                LIMIT ?
                """,
                (after, limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_activity_record_from_row(row) for row in rows)

    def activity_bounds(self) -> tuple[int | None, int]:
        """返回当前保留事件的最小序号和 SQLite 已分配的最高序号。"""

        connection = self._database.connection()
        try:
            row = connection.execute(
                "SELECT MIN(event_seq), MAX(event_seq) FROM workspace_activity"
            ).fetchone()
            sequence_row = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'workspace_activity'"
            ).fetchone()
            first = int(row[0]) if row[0] is not None else None
            latest = int(sequence_row[0]) if sequence_row is not None else 0
            return first, latest
        finally:
            connection.close()

    def prune_activity(self, *, retention_days: int = 30) -> int:
        if retention_days < 1:
            raise ValueError("Workspace 活动事件保留天数必须大于 0")
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        connection = self._database.connection()
        try:
            cursor = connection.execute(
                "DELETE FROM workspace_activity WHERE occurred_at < ?",
                (cutoff,),
            )
            return int(cursor.rowcount)
        finally:
            connection.close()


def _activity_record_from_row(
    row: sqlite3.Row | tuple[object, ...],
) -> WorkspaceActivityRecord:
    """workspace_activity 行投影的唯一实现（追加去重回读与分页读取共用）。"""

    return WorkspaceActivityRecord(
        event_seq=int(row[0]),
        event_id=str(row[1]),
        session_id=str(row[2]),
        status=str(row[3]),
        summary=str(row[4]),
        occurred_at=str(row[5]),
    )


class WorkspaceActivityService:
    def __init__(self, *, workspace_root: Path, retention_days: int = 30) -> None:
        from app.services.infrastructure.workspace_state_store import (
            WorkspaceStateStore,
        )

        self.store = WorkspaceStateStore(workspace_root=workspace_root)
        self.retention_days = retention_days
        self._subscribers: set[asyncio.Queue[WorkspaceActivityRecord]] = set()
        self._subscriber_lock = asyncio.Lock()

    async def append(
        self,
        *,
        event_id: str,
        session_id: str,
        status: str,
        summary: str,
        occurred_at: str | None = None,
    ) -> WorkspaceActivityRecord:
        record = self.store.append_activity(
            event_id=event_id,
            session_id=session_id,
            status=status,
            summary=summary,
            occurred_at=occurred_at,
        )
        async with self._subscriber_lock:
            for subscriber in tuple(self._subscribers):
                try:
                    subscriber.put_nowait(record)
                except asyncio.QueueFull as error:
                    raise RuntimeError("Workspace 活动事件订阅者消费速度不足") from error
        return record

    def list(self, *, after: int = 0, limit: int = 100) -> tuple[WorkspaceActivityRecord, ...]:
        first_seq, latest_seq = self.store.activity_bounds()
        if (
            after > 0
            and latest_seq > after
            and (first_seq is None or first_seq > after + 1)
        ):
            raise WorkspaceActivityCursorGoneError(
                f"Workspace 活动事件游标已失效: after={after}, first={first_seq}"
            )
        records = self.store.list_activity(after=after, limit=limit)
        if after > 0 and records:
            first_seq = records[0].event_seq
            if first_seq > after + 1:
                raise WorkspaceActivityCursorGoneError(
                    f"Workspace 活动事件游标已失效: after={after}, first={first_seq}"
                )
        return records

    async def stream(self, *, after: int = 0):
        async with self._subscriber_lock:
            initial = self.list(after=after)
            subscriber: asyncio.Queue[WorkspaceActivityRecord] = asyncio.Queue(maxsize=100)
            self._subscribers.add(subscriber)
        try:
            for record in initial:
                yield record
            if initial:
                after = initial[-1].event_seq
            while True:
                try:
                    record = await asyncio.wait_for(subscriber.get(), timeout=15)
                except TimeoutError:
                    yield None
                    continue
                if record.event_seq <= after:
                    continue
                after = record.event_seq
                yield record
        finally:
            async with self._subscriber_lock:
                self._subscribers.discard(subscriber)

    def prune(self) -> int:
        return self.store.prune_activity(retention_days=self.retention_days)

    def diagnostics(self) -> SQLiteDiagnostics:
        return self.store.diagnostics()

    def close(self) -> None:
        self.store.close()
