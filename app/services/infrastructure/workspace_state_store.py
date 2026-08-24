from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.sqlite_state import SQLiteDiagnostics, SQLiteStateDatabase, utc_now_text


_WORKSPACE_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS workspace_config (
        config_key TEXT PRIMARY KEY,
        config_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS workspace_activity (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL,
        status TEXT NOT NULL,
        summary TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS workspace_event_cursors (
        cursor_key TEXT PRIMARY KEY,
        cursor_value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
)


@dataclass(frozen=True, slots=True)
class WorkspaceActivityRecord:
    event_seq: int
    event_id: str
    session_id: str
    status: str
    summary: str
    occurred_at: str


@dataclass(frozen=True, slots=True)
class WorkspaceConfigRecord:
    config_key: str
    config_version: int
    payload: dict[str, object]


class WorkspaceActivityCursorGoneError(RuntimeError):
    pass


class WorkspaceActivityService:
    def __init__(self, *, workspace_root: Path, retention_days: int = 30) -> None:
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


class WorkspaceStateStore:
    def __init__(self, *, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.expanduser().resolve()
        self._database = SQLiteStateDatabase(
            path=self.workspace_root / ".boxteam" / "state" / "workspace.sqlite",
            schema_version=len(_WORKSPACE_MIGRATIONS),
            migrations=_WORKSPACE_MIGRATIONS,
        )

    @property
    def path(self) -> Path:
        return self._database.path

    def diagnostics(self) -> SQLiteDiagnostics:
        return self._database.diagnostics()

    def set_config(
        self,
        *,
        config_key: str,
        config_version: int,
        payload: dict[str, object],
    ) -> None:
        connection = self._database.connection()
        try:
            connection.execute(
                """
                INSERT INTO workspace_config(config_key, config_version, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(config_key) DO UPDATE SET
                    config_version=excluded.config_version,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    config_key,
                    config_version,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    utc_now_text(),
                ),
            )
        finally:
            connection.close()

    def get_config(self, config_key: str) -> WorkspaceConfigRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_key, config_version, payload_json
                FROM workspace_config
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(str(row[2]))
            if not isinstance(payload, dict):
                raise ValueError(f"Workspace SQLite 配置不是对象: key={config_key}")
            return WorkspaceConfigRecord(
                config_key=str(row[0]),
                config_version=int(row[1]),
                payload=payload,
            )
        finally:
            connection.close()

    def delete_config(self, config_key: str) -> None:
        connection = self._database.connection()
        try:
            connection.execute(
                "DELETE FROM workspace_config WHERE config_key = ?",
                (config_key,),
            )
        finally:
            connection.close()

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
                return WorkspaceActivityRecord(
                    event_seq=int(existing[0]),
                    event_id=str(existing[1]),
                    session_id=str(existing[2]),
                    status=str(existing[3]),
                    summary=str(existing[4]),
                    occurred_at=str(existing[5]),
                )
            event_seq = int(cursor.lastrowid)
            return WorkspaceActivityRecord(
                event_seq=event_seq,
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
            return tuple(
                WorkspaceActivityRecord(
                    event_seq=int(row[0]),
                    event_id=str(row[1]),
                    session_id=str(row[2]),
                    status=str(row[3]),
                    summary=str(row[4]),
                    occurred_at=str(row[5]),
                )
                for row in rows
            )
        finally:
            connection.close()

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

    def close(self) -> None:
        self._database.close()

    def __enter__(self) -> WorkspaceStateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
