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

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.sqlite_state import SQLITE_BUSY_TIMEOUT_MS
from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    BlobIdentity,
    BlobIdentityConflictError,
    blob_id_for_digest,
    date_bucket_relative_locator,
    utc_bucket_date,
    validate_blob_id,
    validate_blob_relative_locator,
    validate_digest,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    validate_ingest_staging_relative_locator,
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

# catalog 数据库文件名（位于附件 store 根下，与日期分桶同级）。
CATALOG_DATABASE_NAME = "catalog.sqlite"

# ingest record 终态闭集：进入终态后不得再被恢复路径改写。其余状态形态由
# 表的 CHECK 约束单点冻结，不在此重复声明。
INGEST_RECORD_TERMINAL_STATES = ("published", "aborted")

_SCHEMA_VERSION = 1

_BLOBS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS attachment_blobs (
    blob_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    relative_locator TEXT NOT NULL UNIQUE,
    length INTEGER NOT NULL CHECK (length >= 0),
    mime_type TEXT,
    protection TEXT NOT NULL DEFAULT 'private',
    availability TEXT NOT NULL CHECK (availability IN ('available', 'tombstoned')),
    tombstoned_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_INGEST_RECORDS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS attachment_ingest_records (
    ingest_idempotency_key TEXT PRIMARY KEY,
    ingest_id TEXT NOT NULL UNIQUE,
    owner_session_id TEXT NOT NULL,
    owner_thread_id TEXT NOT NULL,
    pin_lease_id TEXT NOT NULL,
    pin_fencing_token INTEGER NOT NULL CHECK (pin_fencing_token >= 1),
    pin_captured_generation INTEGER NOT NULL CHECK (pin_captured_generation >= 0),
    preimage_hash TEXT NOT NULL,
    staging_relative_locator TEXT NOT NULL UNIQUE,
    max_bytes INTEGER NOT NULL CHECK (max_bytes > 0),
    state TEXT NOT NULL CHECK (state IN ('preparing', 'hashed', 'published', 'aborted')),
    digest TEXT,
    blob_id TEXT,
    length INTEGER,
    abort_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CLAIMS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS attachment_blob_commit_claims (
    digest TEXT PRIMARY KEY,
    blob_id TEXT NOT NULL UNIQUE,
    final_relative_locator TEXT NOT NULL UNIQUE,
    expected_length INTEGER NOT NULL CHECK (expected_length >= 0),
    first_claim_utc_date TEXT NOT NULL,
    winning_ingest_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('claimed', 'published', 'aborted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_OWNER_REFS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS attachment_owner_refs (
    owner_ref_id TEXT PRIMARY KEY,
    attachment_id TEXT NOT NULL UNIQUE,
    blob_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    owner_session_id TEXT NOT NULL,
    owner_thread_id TEXT NOT NULL,
    file_name TEXT,
    mime_type TEXT,
    variant TEXT NOT NULL DEFAULT 'original',
    item_ref TEXT,
    retention_until TEXT,
    state TEXT NOT NULL CHECK (state IN ('active', 'released')),
    released_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_IDX_OWNER_REFS_BLOB_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_attachment_owner_refs_blob "
    "ON attachment_owner_refs(blob_id, state)"
)
_IDX_OWNER_REFS_SESSION_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_attachment_owner_refs_session "
    "ON attachment_owner_refs(owner_session_id, state)"
)
_IDX_INGEST_STATE_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_attachment_ingest_records_state "
    "ON attachment_ingest_records(state)"
)
_IDX_INGEST_SESSION_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_attachment_ingest_records_session "
    "ON attachment_ingest_records(owner_session_id, state)"
)

_REQUIRED_TABLES = (
    "attachment_blobs",
    "attachment_ingest_records",
    "attachment_blob_commit_claims",
    "attachment_owner_refs",
)


@dataclass(frozen=True, slots=True)
class AttachmentIngestRecord:
    """``attachment_ingest_records`` 行的不可变投影。"""

    ingest_idempotency_key: str
    ingest_id: str
    owner_session_id: str
    owner_thread_id: str
    pin_lease_id: str
    pin_fencing_token: int
    pin_captured_generation: int
    preimage_hash: str
    staging_relative_locator: str
    max_bytes: int
    state: str
    digest: str | None
    blob_id: str | None
    length: int | None
    abort_reason: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class BlobCommitClaim:
    """``attachment_blob_commit_claims`` 行的不可变投影。"""

    digest: str
    blob_id: str
    final_relative_locator: str
    expected_length: int
    first_claim_utc_date: str
    winning_ingest_id: str
    state: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AttachmentBlobRecord:
    """``attachment_blobs`` 行的不可变投影。"""

    blob_id: str
    digest: str
    relative_locator: str
    length: int
    mime_type: str | None
    protection: str
    availability: str
    tombstoned_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AttachmentOwnerRef:
    """``attachment_owner_refs`` 行的不可变投影。"""

    owner_ref_id: str
    attachment_id: str
    blob_id: str
    digest: str
    owner_session_id: str
    owner_thread_id: str
    file_name: str | None
    mime_type: str | None
    variant: str
    item_ref: str | None
    retention_until: str | None
    state: str
    released_reason: str | None
    created_at: str
    updated_at: str


def derive_attachment_id(
    *,
    blob_id: str,
    owner_session_id: str,
    owner_thread_id: str,
    variant: str = "original",
) -> str:
    """确定性派生逻辑 attachment_id：同一 owner 的同一 blob/variant 幂等复用。

    identity 只由 blob 身份与 owner 决定，不含文件名/MIME/物理 locator；
    重复上传同一正文到同一 owner 得到同一 attachment_id（create-or-get
    owner reference），不同 owner 各自拥有独立 reference。
    """
    validate_blob_id(blob_id)
    if not isinstance(owner_session_id, str) or not owner_session_id:
        raise ValueError(f"owner_session_id 不能为空: {owner_session_id!r}")
    if not isinstance(owner_thread_id, str) or not owner_thread_id:
        raise ValueError(f"owner_thread_id 不能为空: {owner_thread_id!r}")
    if not isinstance(variant, str) or not variant:
        raise ValueError(f"variant 不能为空: {variant!r}")
    preimage = f"{blob_id}|{owner_session_id}|{owner_thread_id}|{variant}"
    payload = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
    return f"att_{payload[:32]}"


def _ingest_record_from_row(row: sqlite3.Row) -> AttachmentIngestRecord:
    return AttachmentIngestRecord(
        ingest_idempotency_key=str(row["ingest_idempotency_key"]),
        ingest_id=str(row["ingest_id"]),
        owner_session_id=str(row["owner_session_id"]),
        owner_thread_id=str(row["owner_thread_id"]),
        pin_lease_id=str(row["pin_lease_id"]),
        pin_fencing_token=int(row["pin_fencing_token"]),
        pin_captured_generation=int(row["pin_captured_generation"]),
        preimage_hash=str(row["preimage_hash"]),
        staging_relative_locator=str(row["staging_relative_locator"]),
        max_bytes=int(row["max_bytes"]),
        state=str(row["state"]),
        digest=None if row["digest"] is None else str(row["digest"]),
        blob_id=None if row["blob_id"] is None else str(row["blob_id"]),
        length=None if row["length"] is None else int(row["length"]),
        abort_reason=(
            None if row["abort_reason"] is None else str(row["abort_reason"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _claim_from_row(row: sqlite3.Row) -> BlobCommitClaim:
    return BlobCommitClaim(
        digest=str(row["digest"]),
        blob_id=str(row["blob_id"]),
        final_relative_locator=str(row["final_relative_locator"]),
        expected_length=int(row["expected_length"]),
        first_claim_utc_date=str(row["first_claim_utc_date"]),
        winning_ingest_id=str(row["winning_ingest_id"]),
        state=str(row["state"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _blob_from_row(row: sqlite3.Row) -> AttachmentBlobRecord:
    return AttachmentBlobRecord(
        blob_id=str(row["blob_id"]),
        digest=str(row["digest"]),
        relative_locator=str(row["relative_locator"]),
        length=int(row["length"]),
        mime_type=None if row["mime_type"] is None else str(row["mime_type"]),
        protection=str(row["protection"]),
        availability=str(row["availability"]),
        tombstoned_at=(
            None if row["tombstoned_at"] is None else str(row["tombstoned_at"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _owner_ref_from_row(row: sqlite3.Row) -> AttachmentOwnerRef:
    return AttachmentOwnerRef(
        owner_ref_id=str(row["owner_ref_id"]),
        attachment_id=str(row["attachment_id"]),
        blob_id=str(row["blob_id"]),
        digest=str(row["digest"]),
        owner_session_id=str(row["owner_session_id"]),
        owner_thread_id=str(row["owner_thread_id"]),
        file_name=None if row["file_name"] is None else str(row["file_name"]),
        mime_type=None if row["mime_type"] is None else str(row["mime_type"]),
        variant=str(row["variant"]),
        item_ref=None if row["item_ref"] is None else str(row["item_ref"]),
        retention_until=(
            None if row["retention_until"] is None else str(row["retention_until"])
        ),
        state=str(row["state"]),
        released_reason=(
            None if row["released_reason"] is None else str(row["released_reason"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class AttachmentBlobCatalog:
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

    # ------------------------------------------------------------------
    # ingest record（preparing → hashed → published / aborted）
    # ------------------------------------------------------------------

    def create_or_get_ingest_record(
        self,
        *,
        ingest_idempotency_key: str,
        ingest_id: str,
        owner_session_id: str,
        owner_thread_id: str,
        pin_lease_id: str,
        pin_fencing_token: int,
        pin_captured_generation: int,
        preimage_hash: str,
        staging_relative_locator: str,
        max_bytes: int,
    ) -> AttachmentIngestRecord:
        """create-or-get ``state=preparing`` record；同 key 不同 preimage 冲突。"""
        _validate_non_empty("ingest_idempotency_key", ingest_idempotency_key)
        _validate_non_empty("pin_lease_id", pin_lease_id)
        _validate_non_empty("owner_session_id", owner_session_id)
        _validate_non_empty("owner_thread_id", owner_thread_id)
        validate_ingest_staging_relative_locator(staging_relative_locator)
        if staging_relative_locator != f".staging/{ingest_id}":
            raise ValueError(
                "staging locator 与 ingest_id 不一致（调用方不得拼路径）: "
                f"staging={staging_relative_locator!r}, ingest_id={ingest_id!r}"
            )
        for name, value in (
            ("pin_fencing_token", pin_fencing_token),
            ("pin_captured_generation", pin_captured_generation),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是 >= 0 的整数: {value!r}")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError(f"max_bytes 必须是正整数: {max_bytes!r}")
        now_text = datetime.now(UTC).isoformat()
        with self.write_transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM attachment_ingest_records "
                "WHERE ingest_idempotency_key = ?",
                (ingest_idempotency_key,),
            ).fetchone()
            if existing is not None:
                record = _ingest_record_from_row(existing)
                if record.preimage_hash != preimage_hash:
                    raise RuntimeError(
                        "同 ingest idempotency key 的 preimage 冲突（fail closed）: "
                        f"key={ingest_idempotency_key!r}"
                    )
                return record
            connection.execute(
                "INSERT INTO attachment_ingest_records ("
                "ingest_idempotency_key, ingest_id, owner_session_id, "
                "owner_thread_id, pin_lease_id, pin_fencing_token, "
                "pin_captured_generation, preimage_hash, staging_relative_locator, "
                "max_bytes, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?, ?)",
                (
                    ingest_idempotency_key,
                    ingest_id,
                    owner_session_id,
                    owner_thread_id,
                    pin_lease_id,
                    pin_fencing_token,
                    pin_captured_generation,
                    preimage_hash,
                    staging_relative_locator,
                    max_bytes,
                    now_text,
                    now_text,
                ),
            )
            inserted = connection.execute(
                "SELECT * FROM attachment_ingest_records "
                "WHERE ingest_idempotency_key = ?",
                (ingest_idempotency_key,),
            ).fetchone()
            return _ingest_record_from_row(inserted)

    def mark_ingest_hashed(
        self,
        *,
        ingest_idempotency_key: str,
        identity: BlobIdentity,
    ) -> tuple[AttachmentIngestRecord, BlobCommitClaim]:
        """事务内推进 record→``hashed`` 并 create-or-get 唯一 digest claim。

        claim 以 digest 唯一约束竞争：已有同 digest claim 时返回胜出 claim
        （record 绑定胜出 blob_id），**不生成第二 locator**；同 blob-id 不同
        length 视为 identity conflict。
        """
        validate_digest(identity.digest)
        validate_blob_id(identity.blob_id)
        if identity.blob_id != blob_id_for_digest(identity.digest):
            raise ValueError(
                "blob_id 与 digest 不匹配（身份三元组必须逐字节一致）: "
                f"blob_id={identity.blob_id!r}, digest={identity.digest!r}"
            )
        # 日期分桶的日期来源必须是显式时间戳（这里取 claim 时刻的显式 UTC
        # datetime），绝不用文件 mtime 或进程本地时区。
        claim_date = utc_bucket_date(datetime.now(UTC))
        relative_locator = date_bucket_relative_locator(identity.blob_id, claim_date)
        now_text = datetime.now(UTC).isoformat()
        with self.write_transaction() as connection:
            record = self._require_ingest_row(connection, ingest_idempotency_key)
            if record.state in INGEST_RECORD_TERMINAL_STATES:
                raise RuntimeError(
                    "ingest record 已终态，拒绝重新推进 hashed: "
                    f"key={ingest_idempotency_key!r}, state={record.state!r}"
                )
            if record.state == "preparing" and identity.length > record.max_bytes:
                raise ValueError(
                    "附件超过 record 冻结的大小限制: "
                    f"length={identity.length}, max_bytes={record.max_bytes}"
                )
            claim_row = connection.execute(
                "SELECT * FROM attachment_blob_commit_claims WHERE digest = ?",
                (identity.digest,),
            ).fetchone()
            if claim_row is None:
                connection.execute(
                    "INSERT INTO attachment_blob_commit_claims ("
                    "digest, blob_id, final_relative_locator, expected_length, "
                    "first_claim_utc_date, winning_ingest_id, state, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?)",
                    (
                        identity.digest,
                        identity.blob_id,
                        relative_locator,
                        identity.length,
                        claim_date.isoformat(),
                        record.ingest_id,
                        now_text,
                        now_text,
                    ),
                )
            else:
                claim = _claim_from_row(claim_row)
                if (
                    claim.blob_id != identity.blob_id
                    or claim.expected_length != identity.length
                ):
                    raise BlobIdentityConflictError(
                        "同一 digest 的 claim 与本次身份不一致（blob-identity-conflict）: "
                        f"digest={identity.digest!r}, "
                        f"claimed_blob_id={claim.blob_id!r}, "
                        f"actual_blob_id={identity.blob_id!r}, "
                        f"claimed_length={claim.expected_length}, "
                        f"actual_length={identity.length}"
                    )
            connection.execute(
                "UPDATE attachment_ingest_records SET state = 'hashed', "
                "digest = ?, blob_id = ?, length = ?, updated_at = ? "
                "WHERE ingest_idempotency_key = ?",
                (
                    identity.digest,
                    identity.blob_id,
                    identity.length,
                    now_text,
                    ingest_idempotency_key,
                ),
            )
            updated = self._require_ingest_row(connection, ingest_idempotency_key)
            claim_row = connection.execute(
                "SELECT * FROM attachment_blob_commit_claims WHERE digest = ?",
                (identity.digest,),
            ).fetchone()
            return updated, _claim_from_row(claim_row)

    def abort_ingest_record(
        self, *, ingest_idempotency_key: str, reason: str
    ) -> AttachmentIngestRecord:
        """把非终态 record 收敛为 ``aborted``（失败清理/删除 drain）。"""
        _validate_non_empty("reason", reason)
        with self.write_transaction() as connection:
            record = self._require_ingest_row(connection, ingest_idempotency_key)
            if record.state in INGEST_RECORD_TERMINAL_STATES:
                return record
            connection.execute(
                "UPDATE attachment_ingest_records SET state = 'aborted', "
                "abort_reason = ?, updated_at = ? "
                "WHERE ingest_idempotency_key = ?",
                (reason, datetime.now(UTC).isoformat(), ingest_idempotency_key),
            )
            return self._require_ingest_row(connection, ingest_idempotency_key)

    def get_ingest_record(self, ingest_idempotency_key: str) -> AttachmentIngestRecord:
        """按 key 读取 record；缺失抛 KeyError。"""
        connection = self._connected()
        return _ingest_record_from_row(
            self._require_ingest_row(connection, ingest_idempotency_key)
        )

    def list_non_terminal_ingest_records(
        self, *, owner_session_id: str | None = None
    ) -> tuple[AttachmentIngestRecord, ...]:
        """按状态索引枚举非终态 record（恢复/删除 drain 只读持久 record）。"""
        connection = self._connected()
        if owner_session_id is None:
            rows = connection.execute(
                "SELECT * FROM attachment_ingest_records "
                "WHERE state NOT IN ('published', 'aborted') "
                "ORDER BY created_at, rowid"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM attachment_ingest_records "
                "WHERE owner_session_id = ? "
                "AND state NOT IN ('published', 'aborted') "
                "ORDER BY created_at, rowid",
                (owner_session_id,),
            ).fetchall()
        return tuple(_ingest_record_from_row(row) for row in rows)

    # ------------------------------------------------------------------
    # blob commit claim
    # ------------------------------------------------------------------

    def get_claim_by_digest(self, digest: str) -> BlobCommitClaim | None:
        """按 digest 取唯一 claim；无 claim 返回 None。"""
        validate_digest(digest)
        row = self._connected().execute(
            "SELECT * FROM attachment_blob_commit_claims WHERE digest = ?",
            (digest,),
        ).fetchone()
        return None if row is None else _claim_from_row(row)

    def get_claim_by_blob_id(self, blob_id: str) -> BlobCommitClaim | None:
        """按 blob-id 取 claim；无 claim 返回 None。"""
        validate_blob_id(blob_id)
        row = self._connected().execute(
            "SELECT * FROM attachment_blob_commit_claims WHERE blob_id = ?",
            (blob_id,),
        ).fetchone()
        return None if row is None else _claim_from_row(row)

    def list_non_terminal_claims(self) -> tuple[BlobCommitClaim, ...]:
        """按状态索引枚举非终态 claim（rename 后未发布崩溃的定点恢复）。"""
        rows = self._connected().execute(
            "SELECT * FROM attachment_blob_commit_claims "
            "WHERE state NOT IN ('published', 'aborted') "
            "ORDER BY created_at, rowid"
        ).fetchall()
        return tuple(_claim_from_row(row) for row in rows)

    def mark_claim_published(self, *, blob_id: str) -> BlobCommitClaim:
        """把 claim 推进为 ``published``（availability + owner ref 同事务已提交）。"""
        validate_blob_id(blob_id)
        with self.write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attachment_blob_commit_claims WHERE blob_id = ?",
                (blob_id,),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"blob commit claim 不存在: blob_id={blob_id!r}, "
                    f"path={self.database_path}"
                )
            claim = _claim_from_row(row)
            if claim.state == "published":
                return claim
            if claim.state == "aborted":
                raise RuntimeError(
                    f"claim 已 aborted，拒绝发布: blob_id={blob_id!r}"
                )
            connection.execute(
                "UPDATE attachment_blob_commit_claims SET state = 'published', "
                "updated_at = ? WHERE blob_id = ?",
                (datetime.now(UTC).isoformat(), blob_id),
            )
            updated = connection.execute(
                "SELECT * FROM attachment_blob_commit_claims WHERE blob_id = ?",
                (blob_id,),
            ).fetchone()
            return _claim_from_row(updated)

    # ------------------------------------------------------------------
    # blob availability + owner reference（可见性提交）
    # ------------------------------------------------------------------

    def get_blob(self, blob_id: str) -> AttachmentBlobRecord | None:
        """按 blob-id 取 blob 记录；无记录返回 None。"""
        validate_blob_id(blob_id)
        row = self._connected().execute(
            "SELECT * FROM attachment_blobs WHERE blob_id = ?", (blob_id,)
        ).fetchone()
        return None if row is None else _blob_from_row(row)

    def get_blob_by_digest(self, digest: str) -> AttachmentBlobRecord | None:
        """按 digest 取 blob 记录；无记录返回 None。"""
        validate_digest(digest)
        row = self._connected().execute(
            "SELECT * FROM attachment_blobs WHERE digest = ?", (digest,)
        ).fetchone()
        return None if row is None else _blob_from_row(row)

    def publish_blob_and_owner_ref(
        self,
        *,
        identity: BlobIdentity,
        relative_locator: str,
        ingest_idempotency_key: str,
        attachment_id: str,
        owner_session_id: str,
        owner_thread_id: str,
        file_name: str | None,
        mime_type: str | None,
        variant: str = "original",
        item_ref: str | None = None,
        retention_until: str | None = None,
        protection: str = "private",
        max_bytes: int | None = None,
    ) -> AttachmentOwnerRef:
        """单事务发布 availability + 逻辑 attachment + owner reference。

        这里是附件可见性的唯一提交点：blob 记录、owner ref 与 claim/
        ingest record 的 terminal 推进在同一 SQLite 事务内完成。同一 blob-id
        与实际 locator/长度不一致时返回 ``blob-identity-conflict``，不覆盖。
        """
        validate_digest(identity.digest)
        validate_blob_id(identity.blob_id)
        validate_blob_relative_locator(relative_locator)
        if relative_locator != date_bucket_relative_locator(
            identity.blob_id,
            _date_from_locator(relative_locator),
        ):
            raise ValueError(
                "relative locator 与 blob-id 不一致（resolver 拒绝调用方拼路径）: "
                f"locator={relative_locator!r}, blob_id={identity.blob_id!r}"
            )
        now_text = datetime.now(UTC).isoformat()
        with self.write_transaction() as connection:
            blob_row = connection.execute(
                "SELECT * FROM attachment_blobs WHERE blob_id = ?",
                (identity.blob_id,),
            ).fetchone()
            if blob_row is None:
                conflict = connection.execute(
                    "SELECT blob_id, length FROM attachment_blobs WHERE digest = ?",
                    (identity.digest,),
                ).fetchone()
                if conflict is not None:
                    raise BlobIdentityConflictError(
                        "同一 digest 已绑定不同 blob-id（blob-identity-conflict）: "
                        f"digest={identity.digest!r}, "
                        f"existing_blob_id={conflict['blob_id']!r}, "
                        f"actual_blob_id={identity.blob_id!r}"
                    )
                connection.execute(
                    "INSERT INTO attachment_blobs (blob_id, digest, "
                    "relative_locator, length, mime_type, protection, "
                    "availability, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'available', ?, ?)",
                    (
                        identity.blob_id,
                        identity.digest,
                        relative_locator,
                        identity.length,
                        mime_type,
                        protection,
                        now_text,
                        now_text,
                    ),
                )
            else:
                blob = _blob_from_row(blob_row)
                if blob.digest != identity.digest or blob.length != identity.length:
                    raise BlobIdentityConflictError(
                        "同一 blob-id 对应不同 digest/length（blob-identity-conflict）: "
                        f"blob_id={identity.blob_id!r}, "
                        f"existing_digest={blob.digest!r}, "
                        f"actual_digest={identity.digest!r}, "
                        f"existing_length={blob.length}, "
                        f"actual_length={identity.length}"
                    )
                if blob.relative_locator != relative_locator:
                    raise BlobIdentityConflictError(
                        "blob 已发布的 relative locator 与本次不一致"
                        "（不得按新日期复制 blob，复用首次 locator）: "
                        f"blob_id={identity.blob_id!r}, "
                        f"existing={blob.relative_locator!r}, "
                        f"actual={relative_locator!r}"
                    )

            record = self._require_ingest_row(connection, ingest_idempotency_key)
            if record.blob_id is not None and record.blob_id != identity.blob_id:
                raise BlobIdentityConflictError(
                    "ingest record 已绑定另一 blob-id（blob-identity-conflict）: "
                    f"key={ingest_idempotency_key!r}, "
                    f"record_blob_id={record.blob_id!r}, "
                    f"actual_blob_id={identity.blob_id!r}"
                )
            if max_bytes is not None and identity.length > max_bytes:
                raise ValueError(
                    "附件超过冻结的大小限制: "
                    f"length={identity.length}, max_bytes={max_bytes}"
                )

            ref_row = connection.execute(
                "SELECT * FROM attachment_owner_refs WHERE attachment_id = ?",
                (attachment_id,),
            ).fetchone()
            if ref_row is not None:
                ref = _owner_ref_from_row(ref_row)
                if (
                    ref.blob_id != identity.blob_id
                    or ref.owner_session_id != owner_session_id
                    or ref.owner_thread_id != owner_thread_id
                ):
                    raise BlobIdentityConflictError(
                        "同 attachment_id 的 owner reference 与本次不一致"
                        "（blob-identity-conflict）: "
                        f"attachment_id={attachment_id!r}, "
                        f"existing_session={ref.owner_session_id!r}, "
                        f"actual_session={owner_session_id!r}"
                    )
                if ref.state == "released":
                    connection.execute(
                        "UPDATE attachment_owner_refs SET state = 'active', "
                        "released_reason = NULL, updated_at = ? "
                        "WHERE owner_ref_id = ?",
                        (now_text, ref.owner_ref_id),
                    )
            else:
                connection.execute(
                    "INSERT INTO attachment_owner_refs (owner_ref_id, "
                    "attachment_id, blob_id, digest, owner_session_id, "
                    "owner_thread_id, file_name, mime_type, variant, item_ref, "
                    "retention_until, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                    (
                        f"oref_{attachment_id.removeprefix('att_')}",
                        attachment_id,
                        identity.blob_id,
                        identity.digest,
                        owner_session_id,
                        owner_thread_id,
                        file_name,
                        mime_type,
                        variant,
                        item_ref,
                        retention_until,
                        now_text,
                        now_text,
                    ),
                )

            connection.execute(
                "UPDATE attachment_ingest_records SET state = 'published', "
                "digest = ?, blob_id = ?, length = ?, updated_at = ? "
                "WHERE ingest_idempotency_key = ?",
                (
                    identity.digest,
                    identity.blob_id,
                    identity.length,
                    now_text,
                    ingest_idempotency_key,
                ),
            )
            connection.execute(
                "UPDATE attachment_blob_commit_claims SET state = 'published', "
                "updated_at = ? WHERE blob_id = ? AND state = 'claimed'",
                (now_text, identity.blob_id),
            )
            published = connection.execute(
                "SELECT * FROM attachment_owner_refs WHERE attachment_id = ?",
                (attachment_id,),
            ).fetchone()
            return _owner_ref_from_row(published)

    def get_owner_ref(self, attachment_id: str) -> AttachmentOwnerRef | None:
        """按逻辑 attachment_id 取 owner reference；无记录返回 None。"""
        _validate_non_empty("attachment_id", attachment_id)
        row = self._connected().execute(
            "SELECT * FROM attachment_owner_refs WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()
        return None if row is None else _owner_ref_from_row(row)

    def list_active_owner_refs_for_blob(
        self, blob_id: str
    ) -> tuple[AttachmentOwnerRef, ...]:
        """列出某 blob 的全部 active owner reference（引用感知 GC 的判据）。"""
        validate_blob_id(blob_id)
        rows = self._connected().execute(
            "SELECT * FROM attachment_owner_refs "
            "WHERE blob_id = ? AND state = 'active' ORDER BY created_at, rowid",
            (blob_id,),
        ).fetchall()
        return tuple(_owner_ref_from_row(row) for row in rows)

    def list_owner_refs_for_session(
        self, owner_session_id: str, *, active_only: bool = False
    ) -> tuple[AttachmentOwnerRef, ...]:
        """按 session 枚举 owner reference（删除 session 只释放其 reference）。"""
        _validate_non_empty("owner_session_id", owner_session_id)
        connection = self._connected()
        if active_only:
            rows = connection.execute(
                "SELECT * FROM attachment_owner_refs WHERE owner_session_id = ? "
                "AND state = 'active' ORDER BY created_at, rowid",
                (owner_session_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM attachment_owner_refs WHERE owner_session_id = ? "
                "ORDER BY created_at, rowid",
                (owner_session_id,),
            ).fetchall()
        return tuple(_owner_ref_from_row(row) for row in rows)

    def release_owner_refs_for_session(
        self, *, owner_session_id: str, reason: str
    ) -> tuple[AttachmentOwnerRef, ...]:
        """释放该 session 的全部 active owner reference（删除 drain 定点调用）。"""
        _validate_non_empty("owner_session_id", owner_session_id)
        _validate_non_empty("reason", reason)
        now_text = datetime.now(UTC).isoformat()
        with self.write_transaction() as connection:
            rows = connection.execute(
                "SELECT owner_ref_id FROM attachment_owner_refs "
                "WHERE owner_session_id = ? AND state = 'active'",
                (owner_session_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE attachment_owner_refs SET state = 'released', "
                    "released_reason = ?, updated_at = ? WHERE owner_ref_id = ?",
                    (reason, now_text, str(row["owner_ref_id"])),
                )
            released = connection.execute(
                "SELECT * FROM attachment_owner_refs WHERE owner_session_id = ? "
                "ORDER BY created_at, rowid",
                (owner_session_id,),
            ).fetchall()
            return tuple(_owner_ref_from_row(row) for row in released)

    # ------------------------------------------------------------------
    # 引用感知 tombstone / GC（零引用 + retention 后先提交 tombstone 再删正文）
    # ------------------------------------------------------------------

    def list_gc_candidates(self, *, before: datetime) -> tuple[AttachmentBlobRecord, ...]:
        """枚举零 active 引用且早于 retention 的 available blob（不扫盘）。"""
        if not isinstance(before, datetime):
            raise TypeError(f"before 必须是 datetime: {before!r}")
        if before.tzinfo is None:
            raise ValueError(f"before 必须带时区: {before!r}")
        rows = self._connected().execute(
            "SELECT b.* FROM attachment_blobs AS b "
            "WHERE b.availability = 'available' AND b.created_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM attachment_owner_refs AS r "
            "  WHERE r.blob_id = b.blob_id AND r.state = 'active') "
            "ORDER BY b.created_at, b.rowid",
            (before.astimezone(UTC).isoformat(),),
        ).fetchall()
        return tuple(_blob_from_row(row) for row in rows)

    def tombstone_blob(self, blob_id: str) -> AttachmentBlobRecord:
        """原子提交 tombstone/availability（必须先行于物理删除，可幂等重试）。"""
        validate_blob_id(blob_id)
        now_text = datetime.now(UTC).isoformat()
        with self.write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attachment_blobs WHERE blob_id = ?", (blob_id,)
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"blob 不存在: blob_id={blob_id!r}, path={self.database_path}"
                )
            blob = _blob_from_row(row)
            if blob.availability == "available":
                connection.execute(
                    "UPDATE attachment_blobs SET availability = 'tombstoned', "
                    "tombstoned_at = ?, updated_at = ? WHERE blob_id = ?",
                    (now_text, now_text, blob_id),
                )
            updated = connection.execute(
                "SELECT * FROM attachment_blobs WHERE blob_id = ?", (blob_id,)
            ).fetchone()
            return _blob_from_row(updated)

    def list_tombstoned_blobs(self) -> tuple[AttachmentBlobRecord, ...]:
        """枚举已提交 tombstone 的 blob（物理删除的幂等重试输入）。"""
        rows = self._connected().execute(
            "SELECT * FROM attachment_blobs WHERE availability = 'tombstoned' "
            "ORDER BY tombstoned_at, rowid"
        ).fetchall()
        return tuple(_blob_from_row(row) for row in rows)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _require_ingest_row(
        self, connection: sqlite3.Connection, ingest_idempotency_key: str
    ) -> AttachmentIngestRecord:
        row = connection.execute(
            "SELECT * FROM attachment_ingest_records "
            "WHERE ingest_idempotency_key = ?",
            (ingest_idempotency_key,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"attachment ingest record 不存在: "
                f"key={ingest_idempotency_key!r}, path={self.database_path}"
            )
        return _ingest_record_from_row(row)


def _validate_non_empty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} 必须是非空字符串: {value!r}")


def _date_from_locator(relative_locator: str):
    """从受检 locator 取日期部分（形态已由 validate 保证）。"""
    year_text, month_text, day_text, _blob = relative_locator.split("/")
    from datetime import date as _date

    return _date(int(year_text), int(month_text), int(day_text))
