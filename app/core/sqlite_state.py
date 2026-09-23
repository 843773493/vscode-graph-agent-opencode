from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final


SQLITE_BUSY_TIMEOUT_MS: Final = 5000

# 迁移文本里的建表/删表语句；用于把「已登记应用的迁移」绑定到它应建出的表。
_CREATE_TABLE_PATTERN: Final = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_DROP_TABLE_PATTERN: Final = re.compile(
    r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _required_tables(
    migrations: tuple[str, ...],
    current: int,
) -> tuple[str, ...]:
    """返回已登记应用的迁移理应留在库中的表。

    只统计第 ``1..current`` 号迁移建出、且未被后续迁移删除的表；后续版本
    建的表由各自的迁移负责，不在此要求。
    """
    created: list[str] = []
    dropped: set[str] = set()
    for migration in migrations[:current]:
        created.extend(_CREATE_TABLE_PATTERN.findall(migration))
        dropped.update(_DROP_TABLE_PATTERN.findall(migration))
    required: list[str] = []
    for table in created:
        if table in dropped or table in required:
            continue
        required.append(table)
    return tuple(required)


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
        allow_shared_processes: bool = False,
    ) -> None:
        if schema_version != len(migrations):
            raise ValueError("SQLite schema_version 必须等于迁移数量")
        self.path = path.expanduser().resolve()
        self._schema_version = schema_version
        self._migrations = migrations
        self._ownership = SQLiteProcessOwnership(self.path)
        self._allow_shared_processes = allow_shared_processes
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._allow_shared_processes:
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
            applied = tuple(
                int(row[0])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            )
            current = applied[-1] if applied else 0
            if current > self._schema_version:
                raise RuntimeError(
                    "SQLite schema 版本高于当前程序支持范围: "
                    f"path={self.path}, current={current}, supported={self._schema_version}"
                )
            # 迁移账本必须是连续的 1..current；任何缺号都说明有人绕过软件
            # 直接改写了 schema_migrations，继续打开会让缺号迁移永久不生效，
            # 因此明确报错而不是静默忽略。
            expected_applied = tuple(range(1, current + 1))
            if applied != expected_applied:
                missing = tuple(
                    version
                    for version in expected_applied
                    if version not in set(applied)
                )
                raise RuntimeError(
                    "SQLite 迁移账本被外部改写，缺少已声明应用的版本: "
                    f"path={self.path}, applied={applied}, missing={missing}"
                )
            if current:
                self._require_migrated_tables(connection, current)
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

    def _require_migrated_tables(
        self,
        connection: sqlite3.Connection,
        current: int,
    ) -> None:
        """校验已登记迁移建出的表确实存在；缺表说明库被外部改写。

        绝不允许以“健康”姿态打开一个缺表的库：由于缺号迁移的建表语句会被
        ``version <= current`` 跳过，表会永久缺失，直到首次真实查询才以
        ``no such table`` 失败。缺表一律响亮失败并列出缺失表名。
        """
        required = _required_tables(self._migrations, current)
        if not required:
            return
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = [table for table in required if table not in present]
        if missing:
            raise RuntimeError(
                "SQLite 迁移账本声明的表缺失，拒绝以缺表状态打开（库被"
                f"外部改写）: path={self.path}, current={current}, "
                f"missing={missing}"
            )

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
