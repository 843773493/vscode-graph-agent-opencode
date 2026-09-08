from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigSourceJournalRecord,
)

SHARED_USER_WORKSPACE_SOURCE_KEY = "workspace:user"


@dataclass(frozen=True, slots=True)
class SourceFanoutRecord:
    source_key: str
    source_generation: int
    workspace_id: str
    status: str
    layer_revision: int | None
    layer_digest: str | None
    result: str | None
    error: str | None
    updated_at: datetime


class WorkspaceSourceOwner:
    """跨 Workspace 进程共享的用户级 Workspace source owner。

    这个数据库故意不使用 SQLiteProcessOwnership：多个独立 Workspace
    backend 必须能够同时观察同一个用户 JSONC，真正的串行边界由 SQLite 的
    ``BEGIN IMMEDIATE`` 和 source_generation CAS 提供。
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS config_source_owner (
        source_key TEXT PRIMARY KEY,
        next_generation INTEGER NOT NULL,
        next_layer_revision INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config_source_journal (
        source_key TEXT NOT NULL,
        source_generation INTEGER NOT NULL,
        source_event_id TEXT NOT NULL UNIQUE,
        source_path TEXT NOT NULL,
        presence TEXT NOT NULL CHECK (presence IN ('present', 'absent')),
        layer_revision INTEGER NOT NULL,
        layer_digest TEXT,
        previous_digest TEXT,
        origin TEXT NOT NULL,
        fanout_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (source_key, source_generation)
    );
    CREATE TABLE IF NOT EXISTS config_source_fanout (
        source_key TEXT NOT NULL,
        source_generation INTEGER NOT NULL,
        workspace_id TEXT NOT NULL,
        status TEXT NOT NULL,
        layer_revision INTEGER,
        layer_digest TEXT,
        result TEXT,
        error TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_key, source_generation, workspace_id),
        FOREIGN KEY (source_key, source_generation)
            REFERENCES config_source_journal(source_key, source_generation)
            ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS config_source_journal_path_idx
        ON config_source_journal(source_key, source_path, source_generation);
    CREATE INDEX IF NOT EXISTS config_source_fanout_workspace_idx
        ON config_source_fanout(source_key, workspace_id, source_generation);
    """

    _VALID_FANOUT_STATUSES = frozenset(
        {"pending", "applying", "applied", "superseded", "conflict", "failed", "blocked"}
    )
    _TERMINAL_FANOUT_STATUSES = frozenset(
        {"applied", "superseded", "conflict", "failed", "blocked"}
    )

    def __init__(self, *, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        if journal_mode.lower() != "wal":
            connection.close()
            raise RuntimeError(
                f"Workspace source owner 无法启用 WAL: path={self.path}, mode={journal_mode}"
            )
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(self._SCHEMA)
        finally:
            connection.close()

    def observe(
        self,
        *,
        source_path: Path,
        presence: str,
        layer_digest: str | None,
        origin: str,
        source_key: str = SHARED_USER_WORKSPACE_SOURCE_KEY,
    ) -> ConfigSourceJournalRecord:
        """记录一次稳定 source 观察；相邻同 digest 观察只返回旧事件。

        只对相邻事件按 digest 去重，因此 ``A -> B -> A`` 必然得到三个
        generation 和三个不同的 fanout 身份。
        """

        if presence not in {"present", "absent"}:
            raise ValueError(f"source owner presence 无效: {presence}")
        if presence == "present" and layer_digest is None:
            raise ValueError("present source owner 观察必须有 layer_digest")
        resolved_path = str(source_path.expanduser().resolve())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                """
                SELECT source_key, source_generation, source_event_id, source_path,
                       presence, layer_revision, layer_digest, previous_digest,
                       origin, fanout_id, created_at
                FROM config_source_journal
                WHERE source_key = ?
                ORDER BY source_generation DESC
                LIMIT 1
                """,
                (source_key,),
            ).fetchone()
            latest_digest = (
                str(latest[6]) if latest is not None and latest[6] is not None else None
            )
            if (
                latest is not None
                and str(latest[3]) == resolved_path
                and str(latest[4]) == presence
                and latest_digest == layer_digest
            ):
                connection.execute("COMMIT")
                return self._journal_from_row(latest)

            owner = connection.execute(
                """
                SELECT next_generation, next_layer_revision
                FROM config_source_owner
                WHERE source_key = ?
                """,
                (source_key,),
            ).fetchone()
            current_generation = int(latest[1]) if latest is not None else 0
            generation = current_generation + 1
            next_layer_revision = (
                int(owner[1]) if owner is not None else 1
            )
            previous_digest = latest_digest
            event_id = f"source-event:{source_key}:{uuid4().hex}"
            fanout_id = f"fanout:{source_key}:generation:{generation}:{uuid4().hex}"
            now = utc_now_text()
            connection.execute(
                """
                INSERT INTO config_source_journal(
                    source_key, source_generation, source_event_id, source_path,
                    presence, layer_revision, layer_digest, previous_digest,
                    origin, fanout_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_key,
                    generation,
                    event_id,
                    resolved_path,
                    presence,
                    next_layer_revision,
                    layer_digest,
                    previous_digest,
                    origin,
                    fanout_id,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO config_source_owner(
                    source_key, next_generation, next_layer_revision
                ) VALUES (?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    next_generation=excluded.next_generation,
                    next_layer_revision=excluded.next_layer_revision
                """,
                (source_key, generation + 1, next_layer_revision + 1),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(source_key=source_key, source_generation=generation)

    def get(
        self,
        *,
        source_key: str,
        source_generation: int,
    ) -> ConfigSourceJournalRecord:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT source_key, source_generation, source_event_id, source_path,
                       presence, layer_revision, layer_digest, previous_digest,
                       origin, fanout_id, created_at
                FROM config_source_journal
                WHERE source_key = ? AND source_generation = ?
                """,
                (source_key, source_generation),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RuntimeError(
                "Workspace source owner 提交后无法读取 journal: "
                f"source_key={source_key}, generation={source_generation}"
            )
        return self._journal_from_row(row)

    def list_journal(
        self,
        *,
        source_key: str = SHARED_USER_WORKSPACE_SOURCE_KEY,
        after_generation: int = 0,
        limit: int = 2000,
    ) -> tuple[ConfigSourceJournalRecord, ...]:
        if after_generation < 0 or not 1 <= limit <= 2000:
            raise ValueError("source owner journal 分页参数无效")
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT source_key, source_generation, source_event_id, source_path,
                       presence, layer_revision, layer_digest, previous_digest,
                       origin, fanout_id, created_at
                FROM config_source_journal
                WHERE source_key = ? AND source_generation > ?
                ORDER BY source_generation ASC
                LIMIT ?
                """,
                (source_key, after_generation, limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._journal_from_row(row) for row in rows)

    def high_water_mark(
        self,
        *,
        source_key: str = SHARED_USER_WORKSPACE_SOURCE_KEY,
    ) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT next_generation FROM config_source_owner WHERE source_key = ?",
                (source_key,),
            ).fetchone()
        finally:
            connection.close()
        return int(row[0]) - 1 if row is not None else 0

    def prepare_fanout(
        self,
        *,
        workspace_id: str,
        source_key: str = SHARED_USER_WORKSPACE_SOURCE_KEY,
        after_generation: int = 0,
        limit: int = 2000,
    ) -> tuple[ConfigSourceJournalRecord, ...]:
        if not workspace_id.strip() or after_generation < 0:
            raise ValueError("source owner fanout workspace 或 high-water 无效")
        records = self.list_journal(
            source_key=source_key,
            after_generation=after_generation,
            limit=limit,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for record in records:
                connection.execute(
                    """
                    INSERT INTO config_source_fanout(
                        source_key, source_generation, workspace_id, status,
                        updated_at
                    ) VALUES (?, ?, ?, 'pending', ?)
                    ON CONFLICT(source_key, source_generation, workspace_id)
                    DO NOTHING
                    """,
                    (
                        record.source_key,
                        record.source_generation,
                        workspace_id,
                        utc_now_text(),
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return records

    def record_fanout(
        self,
        *,
        source_key: str = SHARED_USER_WORKSPACE_SOURCE_KEY,
        source_generation: int,
        workspace_id: str,
        status: str,
        layer_revision: int | None = None,
        layer_digest: str | None = None,
        result: str | None = None,
        error: str | None = None,
    ) -> SourceFanoutRecord:
        if not workspace_id.strip() or status not in self._VALID_FANOUT_STATUSES:
            raise ValueError("source owner fanout 状态或工作区无效")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                """
                SELECT 1 FROM config_source_journal
                WHERE source_key = ? AND source_generation = ?
                """,
                (source_key, source_generation),
            ).fetchone() is None:
                raise ConfigConflictError("source owner fanout journal 不存在")
            existing = connection.execute(
                """
                SELECT status FROM config_source_fanout
                WHERE source_key = ? AND source_generation = ? AND workspace_id = ?
                """,
                (source_key, source_generation, workspace_id),
            ).fetchone()
            if (
                existing is not None
                and str(existing[0]) in self._TERMINAL_FANOUT_STATUSES
                and str(existing[0]) != status
            ):
                # 旧 generation 的冲突/失败结果属于不可覆盖的证据；后续
                # generation 追赶时只补齐缺失记录，不能把它伪装成成功。
                connection.execute("COMMIT")
                return self.get_fanout(
                    source_key=source_key,
                    source_generation=source_generation,
                    workspace_id=workspace_id,
                )
            connection.execute(
                """
                INSERT INTO config_source_fanout(
                    source_key, source_generation, workspace_id, status,
                    layer_revision, layer_digest, result, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key, source_generation, workspace_id) DO UPDATE SET
                    status=excluded.status,
                    layer_revision=excluded.layer_revision,
                    layer_digest=excluded.layer_digest,
                    result=excluded.result,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    source_key,
                    source_generation,
                    workspace_id,
                    status,
                    layer_revision,
                    layer_digest,
                    result,
                    error,
                    utc_now_text(),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_fanout(
            source_key=source_key,
            source_generation=source_generation,
            workspace_id=workspace_id,
        )

    def get_fanout(
        self,
        *,
        source_key: str,
        source_generation: int,
        workspace_id: str,
    ) -> SourceFanoutRecord:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT source_key, source_generation, workspace_id, status,
                       layer_revision, layer_digest, result, error, updated_at
                FROM config_source_fanout
                WHERE source_key = ? AND source_generation = ? AND workspace_id = ?
                """,
                (source_key, source_generation, workspace_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RuntimeError("source owner fanout 提交后无法读取")
        return SourceFanoutRecord(
            source_key=str(row[0]),
            source_generation=int(row[1]),
            workspace_id=str(row[2]),
            status=str(row[3]),
            layer_revision=int(row[4]) if row[4] is not None else None,
            layer_digest=str(row[5]) if row[5] is not None else None,
            result=str(row[6]) if row[6] is not None else None,
            error=str(row[7]) if row[7] is not None else None,
            updated_at=datetime.fromisoformat(str(row[8])),
        )

    def summary(
        self,
        *,
        workspace_ids: tuple[str, ...],
        source_generation: int | None = None,
        source_key: str = SHARED_USER_WORKSPACE_SOURCE_KEY,
    ) -> dict[str, object]:
        if source_generation is None:
            source_generation = self.high_water_mark(source_key=source_key)
        records = {
            workspace_id: self._optional_fanout(
                source_key=source_key,
                source_generation=source_generation,
                workspace_id=workspace_id,
            )
            for workspace_id in workspace_ids
        }
        missing = tuple(
            workspace_id for workspace_id, record in records.items() if record is None
        )
        pending = tuple(
            workspace_id
            for workspace_id, record in records.items()
            if record is not None and record.status in {"pending", "applying"}
        )
        failed = tuple(
            workspace_id
            for workspace_id, record in records.items()
            if record is not None and record.status in {"conflict", "failed", "blocked"}
        )
        result = (
            "fanout_partial"
            if missing or pending or failed
            else "applied"
        )
        return {
            "source_key": source_key,
            "source_generation": source_generation,
            "result": result,
            "missing_workspace_ids": missing,
            "pending_workspace_ids": pending,
            "failed_workspace_ids": failed,
        }

    def _optional_fanout(
        self,
        *,
        source_key: str,
        source_generation: int,
        workspace_id: str,
    ) -> SourceFanoutRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT source_key, source_generation, workspace_id, status,
                       layer_revision, layer_digest, result, error, updated_at
                FROM config_source_fanout
                WHERE source_key = ? AND source_generation = ? AND workspace_id = ?
                """,
                (source_key, source_generation, workspace_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return self.get_fanout(
            source_key=source_key,
            source_generation=source_generation,
            workspace_id=workspace_id,
        )

    @staticmethod
    def _journal_from_row(row: sqlite3.Row) -> ConfigSourceJournalRecord:
        return ConfigSourceJournalRecord(
            source_key=str(row[0]),
            source_generation=int(row[1]),
            source_event_id=str(row[2]),
            source_path=str(row[3]),
            presence=str(row[4]),  # type: ignore[arg-type]
            layer_revision=int(row[5]),
            layer_digest=str(row[6]) if row[6] is not None else None,
            previous_digest=str(row[7]) if row[7] is not None else None,
            origin=str(row[8]),
            fanout_id=str(row[9]),
            created_at=datetime.fromisoformat(str(row[10])),
        )

    def close(self) -> None:
        """保持与其他状态 owner 一致的生命周期接口；本类不持有连接。"""

        return
