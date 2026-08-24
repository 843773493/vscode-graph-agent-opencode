from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final


SQLITE_BUSY_TIMEOUT_MS: Final = 5000


def utc_now_text() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class SQLiteDiagnostics:
    path: str
    schema_version: int
    applied_migrations: tuple[int, ...]
    journal_mode: str
    owner_process_id: int


class SQLiteProcessOwnership:
    """限制一个本地状态库只由一个应用进程持有。"""

    def __init__(self, database_path: Path) -> None:
        self._lock_path = database_path.with_name(f".{database_path.name}.lock")
        self._handle = None

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._lock_path.open("a+", encoding="utf-8")
        if os.name == "nt":
            # TODO: Windows 运行时改为使用等价的原子文件锁实现。
            handle.close()
            raise RuntimeError("Windows 暂不支持 SQLite 状态库进程所有权锁")
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise RuntimeError(
                f"SQLite 状态库已被另一个进程占用: {self._lock_path}"
            ) from error
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        import fcntl

        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class SQLiteStateDatabase:
    """提供带迁移、WAL、外键和诊断的本地 SQLite 基础设施。"""

    def __init__(
        self,
        *,
        path: Path,
        schema_version: int,
        migrations: tuple[str, ...],
    ) -> None:
        if schema_version != len(migrations):
            raise ValueError("SQLite schema_version 必须等于迁移数量")
        self.path = path.expanduser().resolve()
        self._schema_version = schema_version
        self._migrations = migrations
        self._ownership = SQLiteProcessOwnership(self.path)
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ownership.acquire()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError(f"SQLite 状态库已关闭: {self.path}")
        connection = sqlite3.connect(
            self.path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        if journal_mode.lower() != "wal":
            connection.close()
            raise RuntimeError(
                f"SQLite 无法启用 WAL: path={self.path}, journal_mode={journal_mode}"
            )
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            current = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            )
            if current > self._schema_version:
                raise RuntimeError(
                    "SQLite schema 版本高于当前程序支持范围: "
                    f"path={self.path}, current={current}, supported={self._schema_version}"
                )
            for version, migration in enumerate(self._migrations, start=1):
                if version <= current:
                    continue
                try:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        f"{migration}\n"
                        "INSERT INTO schema_migrations(version, applied_at) "
                        f"VALUES ({version}, '{utc_now_text()}');\n"
                        "COMMIT;"
                    )
                except Exception:
                    connection.rollback()
                    raise
        finally:
            connection.close()

    def connection(self) -> sqlite3.Connection:
        return self._connect()

    def diagnostics(self) -> SQLiteDiagnostics:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            versions = tuple(int(row[0]) for row in rows)
            return SQLiteDiagnostics(
                path=str(self.path),
                schema_version=versions[-1] if versions else 0,
                applied_migrations=versions,
                journal_mode=journal_mode,
                owner_process_id=os.getpid(),
            )
        finally:
            connection.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ownership.release()

    def __enter__(self) -> SQLiteStateDatabase:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
