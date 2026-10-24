"""工作区附件目录的唯一 SQLite owner；正文身份不再由 digest 合并。"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.sqlite_state import SQLITE_BUSY_TIMEOUT_MS, validate_sqlite_path_budget
from app.services.infrastructure.attachment_blob_catalog.directory_schema import (
    DIRECTORY_SCHEMA_VERSION,
    initialize_directory_schema,
    require_directory_schema,
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
ATTACHMENT_RETENTION = timedelta(days=30)


class AttachmentDirectoryRetainedError(RuntimeError):
    """目录仍有关联或持久操作依赖，不能开始删除。"""


class AttachmentBlobCatalog(IngestMixin, ForkAttachmentClaimsMixin):
    """沿用原附件 owner 的注入面，线上只读写 schema 4，不回读旧表。"""

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
        with self._connection_guard:
            if self._closed:
                raise RuntimeError("附件目录 catalog 已关闭")
            self._access.acquire()

    def close(self) -> None:
        with self._connection_guard:
            self._closed = True
            for thread_id, connection in tuple(self._connections.items()):
                connection.close()
                del self._connections[thread_id]
            self._access.release()

    def _connected(self) -> sqlite3.Connection:
        # 每个真实执行线程使用独立连接，避免 worker 和事件循环共享同一事务。
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
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
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
        if row is None:
            return None
        record = AttachmentDirectoryRecord(**dict(row))
        validate_attachment_directory_relative_locator(record.relative_locator)
        if record.relative_locator.split("/")[-1] != attachment_id:
            raise RuntimeError("附件目录 locator 与稳定身份不一致")
        return record

    def require_available_directory(self, attachment_id: str) -> AttachmentDirectoryRecord:
        record = self.get_directory(attachment_id)
        if record is None or record.availability != "available":
            raise FileNotFoundError(f"附件目录不可用：attachment_id={attachment_id}")
        return record

    def file_version(self, attachment_id: str, relative_path: str) -> AttachmentFileVersion | None:
        row = self._connected().execute(
            "SELECT * FROM attachment_directory_files WHERE attachment_id=? AND relative_path=?", (attachment_id, relative_path),
        ).fetchone()
        return None if row is None else AttachmentFileVersion(**dict(row))

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
            "UPDATE attachment_directories SET unattached_since=NULL,gc_after=NULL,updated_at=? WHERE attachment_id=?",
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

    @staticmethod
    def _mark_zero_associations(connection: sqlite3.Connection, attachment_id: str, now: datetime) -> None:
        timestamp = now.astimezone(UTC).isoformat()
        deadline = (now.astimezone(UTC) + ATTACHMENT_RETENTION).isoformat()
        connection.execute(
            "UPDATE attachment_directories SET unattached_since=COALESCE(unattached_since,?),gc_after=COALESCE(gc_after,?),updated_at=? "
            "WHERE attachment_id=? AND NOT EXISTS (SELECT 1 FROM attachment_directory_associations a WHERE a.attachment_id=? AND a.state='active')",
            (timestamp, deadline, timestamp, attachment_id, attachment_id),
        )

    def gc_candidates(self, *, now: datetime) -> tuple[AttachmentDirectoryRecord, ...]:
        rows = self._connected().execute(
            "SELECT attachment_id FROM attachment_directories WHERE availability='deleting' OR "
            "(availability='available' AND gc_after<=? AND (retention_until IS NULL OR retention_until<=?)) ORDER BY attachment_id",
            (now.astimezone(UTC).isoformat(), now.astimezone(UTC).isoformat()),
        ).fetchall()
        return tuple(self.get_directory(row[0]) for row in rows)

    def begin_delete(self, attachment_id: str, *, now: datetime) -> AttachmentDirectoryRecord:
        with self.write_transaction() as connection:
            record = self.get_directory(attachment_id)
            if record is None or record.availability == "tombstoned":
                raise FileNotFoundError(f"附件目录不存在：attachment_id={attachment_id}")
            timestamp = now.astimezone(UTC).isoformat()
            if record.availability != "deleting" and (record.gc_after is None or record.gc_after > timestamp or (record.retention_until is not None and record.retention_until > timestamp)):
                raise AttachmentDirectoryRetainedError("附件目录尚未到期")
            association = connection.execute("SELECT 1 FROM attachment_directory_associations WHERE attachment_id=? AND state='active' LIMIT 1", (attachment_id,)).fetchone()
            ingest = connection.execute("SELECT 1 FROM attachment_directory_ingests WHERE attachment_id=? AND state IN ('preparing','durable') LIMIT 1", (attachment_id,)).fetchone()
            fork = connection.execute(
                "SELECT 1 FROM attachment_directory_fork_claims WHERE "
                "(source_attachment_id=? AND export_released=0 AND state NOT IN ('aborted','target_deleted')) OR "
                "(target_attachment_id=? AND state IN ('prepared','owner_reserved')) LIMIT 1", (attachment_id, attachment_id),
            ).fetchone()
            if association is not None or ingest is not None or fork is not None:
                raise AttachmentDirectoryRetainedError("附件目录仍有关联或未结清操作")
            connection.execute("UPDATE attachment_directories SET availability='deleting',updated_at=? WHERE attachment_id=?", (timestamp, attachment_id))
            return self.get_directory(attachment_id)

    def finish_delete(self, attachment_id: str, *, now: datetime) -> None:
        with self.write_transaction() as connection:
            changed = connection.execute(
                "UPDATE attachment_directories SET availability='tombstoned',updated_at=? WHERE attachment_id=? AND availability='deleting'",
                (now.astimezone(UTC).isoformat(), attachment_id),
            ).rowcount
            if not changed and self.get_directory(attachment_id).availability != "tombstoned":
                raise RuntimeError("附件目录没有登记 deleting，不能伪报已回收")
