"""workspace 附件 content-addressed blob catalog（8.3 附件链）。

``<workspace>/.boxteam/attachments/catalog.sqlite`` 是 attachment identity、
digest、受校验 relative locator、length、MIME/protection、variant lineage、
session/thread/item refs、retention、tombstone 与 GC 的唯一权威。reader 只按
catalog 冻结的记录取 locator，**不扫盘、不接受调用方拼出的路径**。

四张表：

- ``attachment_blobs``：digest → 唯一 relative locator、length、MIME/protection、availability/tombstone。
- ``attachment_ingest_records``：以软件生成的 ingest idempotency key 幂等的
  create-or-get record，冻结 pin identity、workspace/preimage、限制与受控
  internal staging locator；本表**不是**可见性权威。
- ``attachment_blob_commit_claims``：以 **digest 唯一约束** 竞争唯一 claim，
  冻结 blob-id、首次 UTC 日期、最终 relative locator 与预期 hash/length。
- ``attachment_owner_refs``：逻辑 attachment/owner reference（session/thread/
  item、variant、retention）；删除 session/thread 只移除对应 reference。

错误分类（沿用 ``session_catalog_store``）：``TypeError`` 类型错、
``ValueError`` 形态非法、``KeyError`` 目标行缺失、``RuntimeError`` 语义冲突
（同 key 不同 preimage、blob identity conflict、库被外部改动）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.sqlite_state import SQLITE_BUSY_TIMEOUT_MS
from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    BlobIdentityConflictError,
)
from app.services.infrastructure.attachment_blob_catalog.blobs import BlobsMixin
from app.services.infrastructure.attachment_blob_catalog.ingest import IngestMixin
from app.services.infrastructure.attachment_blob_catalog.queries import QueriesMixin
from app.services.infrastructure.attachment_blob_catalog.records import (
    AttachmentBlobRecord,
    AttachmentIngestRecord,
    AttachmentOwnerRef,
    BlobCommitClaim,
    derive_attachment_id,
)
from app.services.infrastructure.attachment_blob_catalog.schema import (
    _BLOBS_TABLE_DDL,
    _CLAIMS_TABLE_DDL,
    _IDX_INGEST_SESSION_DDL,
    _IDX_INGEST_STATE_DDL,
    _IDX_OWNER_REFS_BLOB_DDL,
    _IDX_OWNER_REFS_SESSION_DDL,
    _INGEST_RECORDS_TABLE_DDL,
    _OWNER_REFS_TABLE_DDL,
    _REQUIRED_TABLES,
    _SCHEMA_VERSION,
    CATALOG_DATABASE_NAME,
    INGEST_RECORD_TERMINAL_STATES,
)

__all__ = [
    "CATALOG_DATABASE_NAME",
    "INGEST_RECORD_TERMINAL_STATES",
    "AttachmentBlobCatalog",
    "AttachmentBlobRecord",
    "AttachmentIngestRecord",
    "AttachmentOwnerRef",
    "BlobCommitClaim",
    "BlobIdentityConflictError",
    "derive_attachment_id",
]

class AttachmentBlobCatalog(IngestMixin, BlobsMixin, QueriesMixin):
    """附件 blob catalog 的 SQLite owner（唯一权威，绝不扫盘）。"""

    SCHEMA_VERSION = _SCHEMA_VERSION

    def __init__(self, database_path: Path) -> None:
        if not isinstance(database_path, Path):
            raise TypeError(f"database_path 必须是 Path: {database_path!r}")
        self.database_path = database_path.expanduser().resolve()
        self._closed = False
        # 惰性连接：构造不建立目录与数据库文件，首个真实读写才落盘。只读
        # 路径（例如只复验工作区文件附件、只读 fixture 模板）绝不产生副作用。
        self._connection: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # 连接与 schema
    # ------------------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（诊断/注入用）；生产写入走本类方法。"""
        return self._connected()

    def close(self) -> None:
        """关闭连接；重复 close 幂等。"""
        if self._closed:
            return
        self._closed = True
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _connected(self) -> sqlite3.Connection:
        """返回已就绪连接；惰性初始化的唯一入口。"""
        if self._closed:
            raise RuntimeError(f"附件 blob catalog 已关闭: {self.database_path}")
        connection = self._connection
        if connection is not None:
            return connection
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        self._connection = connection
        try:
            self._initialize(connection)
        except BaseException:
            connection.close()
            self._connection = None
            raise
        return connection

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            journal_mode = str(
                connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            )
        except sqlite3.DatabaseError as error:
            connection.close()
            raise RuntimeError(
                "附件 blob catalog 无法打开（文件损坏或被外部改写，须人工核对）: "
                f"path={self.database_path}: {error}"
            ) from error
        if journal_mode.lower() != "wal":
            connection.close()
            raise RuntimeError(
                "附件 blob catalog 无法启用 WAL: "
                f"path={self.database_path}, journal_mode={journal_mode}"
            )
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        """幂等建表并写 user_version；未知版本 fail-closed。"""
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current > self.SCHEMA_VERSION:
            raise RuntimeError(
                "附件 blob catalog schema 版本未知，fail-closed 拒绝打开: "
                f"path={self.database_path}, user_version={current}, "
                f"supported={self.SCHEMA_VERSION}"
            )
        if current:
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing = [t for t in _REQUIRED_TABLES if t not in present]
            if missing:
                raise RuntimeError(
                    "附件 blob catalog 缺表，拒绝以空表重建（库被外部改写）: "
                    f"path={self.database_path}, missing={missing}"
                )
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(_BLOBS_TABLE_DDL)
            connection.execute(_INGEST_RECORDS_TABLE_DDL)
            connection.execute(_IDX_INGEST_STATE_DDL)
            connection.execute(_IDX_INGEST_SESSION_DDL)
            connection.execute(_CLAIMS_TABLE_DDL)
            connection.execute(_OWNER_REFS_TABLE_DDL)
            connection.execute(_IDX_OWNER_REFS_BLOB_DDL)
            connection.execute(_IDX_OWNER_REFS_SESSION_DDL)
            if current != self.SCHEMA_VERSION:
                connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """单个写事务：BEGIN IMMEDIATE，异常回滚。"""
        connection = self._connected()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
