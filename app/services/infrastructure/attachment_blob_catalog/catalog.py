"""工作区附件目录的唯一 SQLite owner；正文身份不再由 digest 合并。"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.core.atomic_fs import native_io_path
from app.core.failures import mark_public_failure
from app.core.sqlite_state import (
    SQLITE_BUSY_TIMEOUT_MS,
    sqlite_file_uri,
    validate_sqlite_path_budget,
)
from app.services.infrastructure.attachment_blob_catalog.directory_schema import (
    DIRECTORY_SCHEMA_VERSION,
    initialize_directory_schema,
    require_directory_schema,
    upgrade_directory_schema_4,
)
from app.services.infrastructure.attachment_blob_catalog.fork_claims import (
    ForkAttachmentClaimsMixin,
)
from app.services.infrastructure.attachment_blob_catalog.ingest import IngestMixin
from app.services.infrastructure.attachment_blob_catalog.locator import (
    validate_attachment_directory_relative_locator,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    AttachmentDirectoryRecord,
    AttachmentFileVersion,
)

CATALOG_DATABASE_NAME = "catalog.sqlite"


class AttachmentDirectoryRetainedError(RuntimeError):
    """目录仍有关联或持久操作依赖，不能开始删除。"""


def _directory_record(row: sqlite3.Row) -> AttachmentDirectoryRecord:
    """查询结果统一校验 locator，保留 SQLite 已读出的同一行快照。"""
    record = AttachmentDirectoryRecord(**dict(row))
    validate_attachment_directory_relative_locator(record.relative_locator)
    if record.relative_locator.split("/")[-1] != record.attachment_id:
        raise RuntimeError("附件目录 locator 与稳定身份不一致")
    return record


class AttachmentBlobCatalog(IngestMixin, ForkAttachmentClaimsMixin):
    """沿用原附件 owner 的注入面，线上只读写当前目录结构，不回读旧表。"""

    SCHEMA_VERSION = DIRECTORY_SCHEMA_VERSION

    def __init__(self, database_path: Path, *, workspace_id: str) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.workspace_id = workspace_id
        self._closed = False
        self._connections: dict[int, sqlite3.Connection] = {}
        self._connection_guard = threading.RLock()
        self._access = AttachmentMaintenanceLock(self.database_path.parent, exclusive=False)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connected()

    def acquire_access(self) -> None:
        if self._closed:
            raise RuntimeError("附件目录 catalog 已关闭")
        with self._connection_guard:
            if self._closed:
                raise RuntimeError("附件目录 catalog 已关闭")
            self._access.acquire()

    def close_admission(self) -> None:
        """同步撤销准入；原关闭在线程池排队或等待连接时也不再接纳访问。"""
        self._closed = True

    def close(self) -> None:
        self.close_admission()
        with self._connection_guard:
            for thread_id, connection in tuple(self._connections.items()):
                connection.close()
                del self._connections[thread_id]
            self._access.release()

    def _connected(self) -> sqlite3.Connection:
        # 每个真实执行线程使用独立连接，避免 worker 和事件循环共享同一事务。
        if self._closed:
            raise RuntimeError("附件目录 catalog 已关闭")
        with self._connection_guard:
            self.acquire_access()
            thread_id = threading.get_ident()
            if thread_id not in self._connections:
                try:
                    self._connections[thread_id] = self._open_connection()
                except BaseException:
                    if not self._connections:
                        self._access.release()
                    raise
            return self._connections[thread_id]

    def _open_connection(self) -> sqlite3.Connection:
        validate_sqlite_path_budget(self.database_path)
        native_io_path(self.database_path.parent).mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            sqlite_file_uri(self.database_path), uri=True, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None, check_same_thread=False,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                initialize_directory_schema(connection, workspace_id=self.workspace_id)
            else:
                if version == 4:
                    upgrade_directory_schema_4(connection, workspace_id=self.workspace_id)
                    version = self.SCHEMA_VERSION
                if version != self.SCHEMA_VERSION:
                    raise RuntimeError(
                        f"附件目录只支持 schema {self.SCHEMA_VERSION}，当前 version={version}；"
                        "旧 schema 3 须停止工作区后经显式离线迁移，原数据不变"
                    )
                require_directory_schema(connection)
            metadata = connection.execute("SELECT workspace_id FROM attachment_directory_metadata WHERE singleton_id=1").fetchone()
            if metadata is None or metadata[0] != self.workspace_id:
                raise RuntimeError("附件目录 catalog 的工作区归属不一致")
            if str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
                raise RuntimeError("附件目录 catalog 无法启用 WAL")
            connection.execute("PRAGMA synchronous=FULL")
        except BaseException as error:
            try:
                connection.close()
            except BaseException as close_error:  # noqa: BLE001 - 保留原异常及未关闭连接
                self._closed = True
                self._connections[threading.get_ident()] = connection
                error.add_note(f"初始化失败且连接关闭未核实，保留维护锁：{close_error!r}")
            raise
        return connection

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connected()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except BaseException as rollback_error:  # noqa: BLE001 - 回滚失败不能替代业务原错
                error.add_note(f"附件目录事务回滚失败：{rollback_error!r}")
                try:
                    self.close()
                except BaseException as close_error:  # noqa: BLE001 - 原错优先，连接未关闭仍可重试
                    error.add_note(f"附件目录连接关闭也失败：{close_error!r}")
            raise

    def get_directory(self, attachment_id: str) -> AttachmentDirectoryRecord | None:
        row = self._connected().execute("SELECT * FROM attachment_directories WHERE attachment_id=?", (attachment_id,)).fetchone()
        return None if row is None else _directory_record(row)

    def require_available_directory(self, attachment_id: str) -> AttachmentDirectoryRecord:
        record = self.get_directory(attachment_id)
        if record is None or record.availability != "available":
            error = FileNotFoundError(f"附件目录不可用：attachment_id={attachment_id}")
            raise mark_public_failure(error, str(error))
        return record

    def file_version(self, attachment_id: str, relative_path: str) -> AttachmentFileVersion | None:
        row = self._connected().execute(
            "SELECT * FROM attachment_directory_files WHERE attachment_id=? AND relative_path=?", (attachment_id, relative_path),
        ).fetchone()
        return None if row is None else AttachmentFileVersion(**dict(row))

    def thread_originals(self, session_id: str, thread_id: str, *, after: str, limit: int) -> tuple[tuple[AttachmentFileVersion, str], ...]:
        """一次查询原关联和原件元数据；已释放工作副本仍保留历史来源。"""
        rows = self._connected().execute(
            "SELECT f.*,d.availability FROM attachment_directory_associations a "
            "JOIN attachment_directories d ON d.attachment_id=a.attachment_id "
            "JOIN attachment_directory_files f ON f.attachment_id=a.attachment_id AND f.role='original' "
            "WHERE a.session_id=? AND a.thread_id=? AND a.attachment_id>? "
            "ORDER BY a.attachment_id LIMIT ?", (session_id, thread_id, after, limit),
        )
        originals = []
        for row in rows:
            values = dict(row)
            availability = values.pop("availability")
            originals.append((AttachmentFileVersion(**values), availability))
        return tuple(originals)

    def publish_file_version(self, version: AttachmentFileVersion) -> None:
        """原件与预览都是不可变版本；同一登记位置出现不同内容必须报错。"""
        with self.write_transaction() as connection:
            self.require_available_directory(version.attachment_id)
            previous = self.file_version(version.attachment_id, version.relative_path)
            if previous is not None:
                if previous != version:
                    raise RuntimeError("附件原件或预览版本冲突，不能覆盖已登记内容")
                return
            self._insert_file_version(connection, version)

    @staticmethod
    def _insert_file_version(connection: sqlite3.Connection, version: AttachmentFileVersion) -> None:
        connection.execute(
            "INSERT INTO attachment_directory_files VALUES (?,?,?,?,?,?,?)",
            (version.attachment_id, version.relative_path, version.role, version.digest, version.length, version.file_name, version.mime_type),
        )

    def attach_directory(self, attachment_id: str, *, session_id: str, thread_id: str, binding_token: str, now: datetime) -> None:
        if not binding_token:
            raise ValueError("目录关联必须携带真实代次或操作 token")
        with self.write_transaction() as connection:
            self.require_available_directory(attachment_id)
            self._attach(connection, attachment_id, session_id, thread_id, binding_token, now)

    @staticmethod
    def _attach(connection: sqlite3.Connection, attachment_id: str, session_id: str, thread_id: str, binding_token: str, now: datetime) -> None:
        timestamp = now.astimezone(UTC).isoformat()
        connection.execute(
            "INSERT INTO attachment_directory_associations VALUES (?,?,?,?, 'active',?,?) "
            "ON CONFLICT(attachment_id,session_id,thread_id) DO UPDATE SET "
            "binding_token=excluded.binding_token,state='active',updated_at=excluded.updated_at",
            (attachment_id, session_id, thread_id, binding_token, timestamp, timestamp),
        )
        connection.execute(
            "UPDATE attachment_directories SET release_pending=0,release_reason=NULL,updated_at=? WHERE attachment_id=?",
            (timestamp, attachment_id),
        )

    def release_associations(self, *, session_id: str, thread_id: str | None = None, binding_token: str | None = None, now: datetime) -> tuple[str, ...]:
        """删除 owner 可释放整个精确 scope；代次卸载必须带 token，迟到释放不会误删新关联。"""
        with self.write_transaction() as connection:
            conditions = ["session_id=?", "state='active'"]
            values: list[str] = [session_id]
            if thread_id is not None:
                conditions.append("thread_id=?")
                values.append(thread_id)
            if binding_token is not None:
                conditions.append("binding_token=?")
                values.append(binding_token)
            where = " AND ".join(conditions)
            ids = tuple(row[0] for row in connection.execute(f"SELECT attachment_id FROM attachment_directory_associations WHERE {where}", values))
            connection.execute(f"UPDATE attachment_directory_associations SET state='released',updated_at=? WHERE {where}", [now.astimezone(UTC).isoformat(), *values])
            for attachment_id in ids:
                self._mark_zero_associations(connection, attachment_id, now)
            return ids

    def bind_thread_runtime(self, session_id: str, thread_id: str, binding_token: str, *, now: datetime) -> None:
        """只换绑仍活动的关联，不从完整历史复活已解除或已回收目录。"""
        if not binding_token.startswith("runtime:"):
            raise ValueError("runtime 绑定必须携带原 tracker 的跨重启 token")
        with self.write_transaction() as connection:
            connection.execute(
                "UPDATE attachment_directory_associations SET binding_token=?,updated_at=? "
                "WHERE session_id=? AND thread_id=? AND state='active'",
                (binding_token, now.astimezone(UTC).isoformat(), session_id, thread_id),
            )

    def runtime_bindings(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(tuple(row) for row in self._connected().execute(
            "SELECT DISTINCT session_id,thread_id,binding_token FROM attachment_directory_associations "
            "WHERE state='active' AND binding_token LIKE 'runtime:%' ORDER BY session_id,thread_id,binding_token",
        ))

    def has_runtime_binding(self, session_id: str, thread_id: str, token: str) -> bool:
        return self._connected().execute(
            "SELECT 1 FROM attachment_directory_associations WHERE session_id=? AND thread_id=? "
            "AND binding_token=? AND state='active' LIMIT 1", (session_id, thread_id, token),
        ).fetchone() is not None

    @staticmethod
    def _mark_zero_associations(connection: sqlite3.Connection, attachment_id: str, now: datetime) -> None:
        timestamp = now.astimezone(UTC).isoformat()
        connection.execute(
            "UPDATE attachment_directories SET release_pending=1,release_reason=NULL,updated_at=? "
            "WHERE attachment_id=? AND availability NOT IN ('released','tombstoned') "
            "AND NOT EXISTS (SELECT 1 FROM attachment_directory_associations a WHERE a.attachment_id=? AND a.state='active')",
            (timestamp, attachment_id, attachment_id),
        )

    def pending_directories(self, *, attachment_ids: Sequence[str] | None = None) -> tuple[AttachmentDirectoryRecord, ...]:
        if attachment_ids is not None and not attachment_ids:
            return ()
        selected = "" if attachment_ids is None else f" AND attachment_id IN ({','.join('?' for _ in attachment_ids)})"
        rows = self._connected().execute(
            "SELECT * FROM attachment_directories WHERE (release_pending=1 OR availability IN ('releasing','rebuilding'))"
            + selected + " ORDER BY attachment_id", () if attachment_ids is None else tuple(attachment_ids),
        ).fetchall()
        return tuple(_directory_record(row) for row in rows)

    def require_releasable_directory(self, attachment_id: str, *, now: datetime) -> AttachmentDirectoryRecord:
        """调用方已持原目录 holder；事务内复核全部持久依赖，不按时间猜占用终态。"""
        with self.write_transaction() as connection:
            record = self.get_directory(attachment_id)
            if record is None or record.availability == "tombstoned":
                raise FileNotFoundError(f"附件目录不存在：attachment_id={attachment_id}")
            timestamp = now.astimezone(UTC).isoformat()
            if record.retention_until is not None and record.retention_until > timestamp:
                raise AttachmentDirectoryRetainedError("附件目录仍有明确保留承诺")
            association = connection.execute("SELECT 1 FROM attachment_directory_associations WHERE attachment_id=? AND state='active' LIMIT 1", (attachment_id,)).fetchone()
            ingest = connection.execute("SELECT 1 FROM attachment_directory_ingests WHERE attachment_id=? AND state IN ('preparing','durable') LIMIT 1", (attachment_id,)).fetchone()
            fork = connection.execute(
                "SELECT 1 FROM attachment_directory_fork_claims WHERE "
                # 同目录复用在发布前由目标 claim、发布后由关联保活，不等待不存在的跨端导出回执。
                "(source_attachment_id=? AND source_attachment_id<>target_attachment_id "
                "AND export_released=0 AND state NOT IN ('aborted','target_deleted')) OR "
                "(target_attachment_id=? AND state IN ('prepared','owner_reserved')) LIMIT 1", (attachment_id, attachment_id),
            ).fetchone()
            if association is not None or ingest is not None or fork is not None:
                raise AttachmentDirectoryRetainedError("附件目录仍有关联或未结清操作")
            return record

    def set_working_copy_state(self, attachment_id: str, *, expected: str, state: str, now: datetime) -> None:
        """释放与重建只改变工作副本事实，资源身份和不可变版本永不变成新资源。"""
        allowed = {("available", "releasing"), ("releasing", "released"), ("released", "rebuilding"), ("rebuilding", "available")}
        if (expected, state) not in allowed:
            raise ValueError("附件工作副本状态转换非法")
        with self.write_transaction() as connection:
            changed = connection.execute(
                "UPDATE attachment_directories SET availability=?,release_pending=0,release_reason=NULL,updated_at=? WHERE attachment_id=? AND availability=?",
                (state, now.astimezone(UTC).isoformat(), attachment_id, expected),
            ).rowcount
            if not changed:
                record = self.get_directory(attachment_id)
                if record is None or record.availability != state:
                    raise RuntimeError("附件工作副本状态前像冲突")

    def request_working_copy_check(self, attachment_id: str, *, now: datetime) -> None:
        with self.write_transaction() as connection:
            self._mark_zero_associations(connection, attachment_id, now)

    def retain_working_copy(self, attachment_id: str, *, reason: str, now: datetime, pending: bool = True) -> None:
        with self.write_transaction() as connection:
            connection.execute(
                "UPDATE attachment_directories SET release_pending=?,release_reason=?,updated_at=? WHERE attachment_id=? AND availability<>'tombstoned'",
                (int(pending), reason, now.astimezone(UTC).isoformat(), attachment_id),
            )
