from __future__ import annotations

import _sqlite3
import ctypes
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import BinaryIO, Final, Self
from urllib.parse import quote

import portalocker

from app.core.atomic_fs import native_io_path

SQLITE_BUSY_TIMEOUT_MS: Final = 5000


def load_sql(package: str, resource: str) -> str:
    """读取随 owner 分发的 SQL；资产缺失或不可读时直接报错。"""
    return files(package).joinpath(resource).read_text(encoding="utf-8")


class _SQLiteVFSHeader(ctypes.Structure):
    """SQLite VFS 稳定头部；读取 mxPathname 所需的最小 ABI 前缀。"""

    _fields_ = [
        ("iVersion", ctypes.c_int),
        ("szOsFile", ctypes.c_int),
        ("mxPathname", ctypes.c_int),
    ]


def sqlite_vfs_max_pathname() -> int:
    """返回当前 Python SQLite 默认 VFS 的路径上限，能力不可用时明确报错。"""
    extension_path = getattr(_sqlite3, "__file__", None)
    if _sqlite3.__spec__ is not None and _sqlite3.__spec__.origin == "built-in":
        # standalone Python 隐藏了静态 SQLite 符号；扩展通过当前连接提供的
        # SQLite API 查询同一 VFS，不能加载系统中另一份 SQLite 代替它。
        probe = files("app.core").joinpath("sqlite_vfs_probe.so")
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.enable_load_extension(True)
            try:
                connection.load_extension(str(probe))
            finally:
                connection.enable_load_extension(False)
            return int(connection.execute("SELECT boxteam_vfs_max_pathname()").fetchone()[0])
    if not isinstance(extension_path, str) or not extension_path:
        raise RuntimeError(
            "无法定位当前 Python SQLite 扩展，不能读取默认 VFS mxPathname: "
            f"sqlite_version={sqlite3.sqlite_version}, extension={extension_path!r}"
        )
    try:
        if os.name == "nt":
            # CPython 的 Windows 扩展从已加载的 sqlite3.dll 导入符号；
            # 不从 PATH 搜索并加载另一份 SQLite，否则会查询错误的 VFS 注册表。
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            get_module = kernel.GetModuleHandleW
            get_module.argtypes = [ctypes.c_wchar_p]
            get_module.restype = ctypes.c_void_p
            handle = get_module("sqlite3.dll")
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            native_sqlite = ctypes.CDLL("sqlite3.dll", handle=handle)
        else:
            native_sqlite = ctypes.CDLL(extension_path)
    except OSError as error:
        raise RuntimeError(
            "无法加载当前 Python SQLite 扩展以读取默认 VFS mxPathname: "
            f"sqlite_version={sqlite3.sqlite_version}, extension={extension_path!r}, "
            f"error={error}"
        ) from error
    try:
        find_vfs = native_sqlite.sqlite3_vfs_find
    except AttributeError as error:
        raise RuntimeError(
            "当前 Python SQLite 未导出 sqlite3_vfs_find，不能读取默认 VFS "
            f"mxPathname: sqlite_version={sqlite3.sqlite_version}, "
            f"extension={extension_path!r}"
        ) from error
    find_vfs.argtypes = [ctypes.c_char_p]
    find_vfs.restype = ctypes.POINTER(_SQLiteVFSHeader)
    header = find_vfs(None)
    if not header:
        raise RuntimeError(
            "当前 Python SQLite 未注册默认 VFS，不能校验数据库路径: "
            f"sqlite_version={sqlite3.sqlite_version}, extension={extension_path!r}"
        )
    value = header.contents
    if value.iVersion < 1 or value.szOsFile < 0 or value.mxPathname < 1:
        raise RuntimeError(
            "当前 Python SQLite 默认 VFS 头部字段非法，不能校验数据库路径: "
            f"sqlite_version={sqlite3.sqlite_version}, extension={extension_path!r}, "
            f"iVersion={value.iVersion}, szOsFile={value.szOsFile}, "
            f"mxPathname={value.mxPathname}"
        )
    return value.mxPathname


def sqlite_file_uri(database_path: Path) -> str:
    """编码实际交给 VFS 的原生路径；查询参数由连接 owner 显式附加。"""
    # Windows 扩展路径必须作为 URI 路径编码，不能由 as_uri 把 ? 当作 authority。
    return "file:" + quote(str(native_io_path(database_path.absolute())), safe="/:")


def validate_sqlite_path_budget(database_path: Path) -> None:
    """校验 SQLite 主库及 rollback/WAL sidecar 的绝对 UTF-8 路径预算。"""
    if not isinstance(database_path, Path):
        raise TypeError(f"database_path 必须是 Path: {database_path!r}")
    absolute_path = native_io_path(database_path.expanduser().resolve())
    maximum = sqlite_vfs_max_pathname()
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = f"{absolute_path}{suffix}"
        path_bytes = len(candidate.encode("utf-8"))
        if path_bytes > maximum:
            raise ValueError(
                "SQLite 绝对路径超出当前默认 VFS 预算: "
                f"path={candidate}, bytes={path_bytes}, mxPathname={maximum}, "
                f"sqlite_version={sqlite3.sqlite_version}"
            )

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
    migrations: tuple[Migration, ...],
    current: int,
) -> tuple[str, ...]:
    """返回已登记应用的迁移理应留在库中的表。

    只统计第 ``1..current`` 号迁移建出、且未被后续迁移删除的表；后续版本
    建的表由各自的迁移负责，不在此要求。
    """
    created: list[str] = []
    dropped: set[str] = set()
    for migration in migrations[:current]:
        if not isinstance(migration, str):
            continue
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


# 一次迁移可以是一条 DDL/迁移 SQL 文本，也可以是需要过程化判定（读旧表、
# 校验一致性、按行重建）的 Python 回调；二者都在同一事务内与 schema_migrations
# 登记一起提交。回调只接收一个连接，不接收其它上下文。
Migration = str | Callable[[sqlite3.Connection], None]


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
        self._lock_handle: BinaryIO | None = None

    def acquire(self) -> None:
        native_io_path(self._lock_path.parent).mkdir(parents=True, exist_ok=True)
        handle = native_io_path(self._lock_path).open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            portalocker.lock(
                handle, portalocker.LOCK_EX | portalocker.LOCK_NB
            )
        except portalocker.AlreadyLocked as error:
            handle.close()
            raise RuntimeError(
                f"SQLite 状态库已被另一个进程占用: {self._lock_path}"
            ) from error
        except BaseException:
            handle.close()
            raise
        self._lock_handle = handle

    def release(self) -> None:
        handle = self._lock_handle
        if handle is None:
            return
        self._lock_handle = None
        try:
            portalocker.unlock(handle)
        finally:
            handle.close()


class SQLiteStateDatabase:
    """提供带迁移、WAL、外键和诊断的本地 SQLite 基础设施。"""

    def __init__(
        self,
        *,
        path: Path,
        schema_version: int,
        migrations: tuple[Migration, ...],
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
        validate_sqlite_path_budget(self.path)
        native_io_path(self.path.parent).mkdir(parents=True, exist_ok=True)
        # 构造期任一步骤失败都必须归还进程所有权锁：异常对象会连同
        # traceback 一起被调用方长期持有，锁 fd 若随之存活，调用方重试
        # 同一路径时只会看到「已被另一个进程占用」，真实迁移错误被掩盖。
        acquired = False
        try:
            if not self._allow_shared_processes:
                self._ownership.acquire()
                acquired = True
            self._initialize()
        except BaseException:
            if acquired:
                self._ownership.release()
            raise

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError(f"SQLite 状态库已关闭: {self.path}")
        connection = sqlite3.connect(
            sqlite_file_uri(self.path), uri=True,
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
                load_sql(__package__, "sqlite_state.sql")
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
                    if isinstance(migration, str):
                        connection.executescript(
                            "BEGIN IMMEDIATE;\n"
                            f"{migration}\n"
                            "INSERT INTO schema_migrations(version, applied_at) "
                            f"VALUES ({version}, '{utc_now_text()}');\n"
                            "COMMIT;"
                        )
                    else:
                        connection.execute("BEGIN IMMEDIATE")
                        migration(connection)
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) "
                            "VALUES (?, ?)",
                            (version, utc_now_text()),
                        )
                        connection.execute("COMMIT")
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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
