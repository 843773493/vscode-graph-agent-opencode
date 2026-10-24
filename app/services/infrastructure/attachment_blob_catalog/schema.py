"""仅供显式离线迁移核对 schema 3；线上只使用 directory_schema 的 schema 4。"""

from __future__ import annotations

from app.core.sqlite_state import load_sql

__all__ = [
    "CATALOG_DATABASE_NAME",
    "INGEST_RECORD_TERMINAL_STATES",
]


# catalog 数据库文件名（位于附件 store 根下，与日期分桶同级）。
CATALOG_DATABASE_NAME = "catalog.sqlite"

# ingest record 终态闭集：进入终态后不得再被恢复路径改写。其余状态形态由
# 表的 CHECK 约束单点冻结，不在此重复声明。
INGEST_RECORD_TERMINAL_STATES = ("published", "aborted")

_SCHEMA_VERSION = 3

_BLOBS_TABLE_DDL = load_sql(__package__, "sql/attachment_blobs.sql")

_INGEST_RECORDS_TABLE_DDL = load_sql(__package__, "sql/attachment_ingest_records.sql")

_CLAIMS_TABLE_DDL = load_sql(__package__, "sql/attachment_blob_commit_claims.sql")

_OWNER_REFS_TABLE_DDL = load_sql(__package__, "sql/attachment_owner_refs.sql")

_FORK_CLAIMS_TABLE_DDL = load_sql(__package__, "sql/fork_attachment_claims.sql")

_FORK_CLAIMS_INDEX_DDL = load_sql(__package__, "sql/idx_fork_attachment_claims_blob.sql")

_FORK_MAPPINGS_TABLE_DDL = load_sql(__package__, "sql/fork_attachment_mappings.sql")

_FORK_MAPPINGS_INDEX_DDL = load_sql(__package__, "sql/fork_attachment_mappings_target.sql")

_REVISION_DDL = (
    load_sql(__package__, "sql/attachment_catalog_revision.sql"),
    load_sql(__package__, "sql/attachment_catalog_revision_seed.sql"),
    load_sql(__package__, "sql/attachment_revision_attachment_blobs_insert.sql"),
    load_sql(__package__, "sql/attachment_revision_attachment_blobs_update.sql"),
    load_sql(__package__, "sql/attachment_revision_attachment_blobs_delete.sql"),
    load_sql(__package__, "sql/attachment_revision_attachment_owner_refs_insert.sql"),
    load_sql(__package__, "sql/attachment_revision_attachment_owner_refs_update.sql"),
    load_sql(__package__, "sql/attachment_revision_attachment_owner_refs_delete.sql"),
)

_IDX_OWNER_REFS_BLOB_DDL = (
    load_sql(__package__, "sql/idx_attachment_owner_refs_blob.sql")
)
_IDX_OWNER_REFS_SESSION_DDL = (
    load_sql(__package__, "sql/idx_attachment_owner_refs_session.sql")
)
_IDX_INGEST_STATE_DDL = (
    load_sql(__package__, "sql/idx_attachment_ingest_records_state.sql")
)
_IDX_INGEST_SESSION_DDL = (
    load_sql(__package__, "sql/idx_attachment_ingest_records_session.sql")
)

# 线上初始化与离线 schema 参照使用同一顺序，不复制表、约束和触发器定义。
_SCHEMA_STATEMENTS = (
    _BLOBS_TABLE_DDL,
    _INGEST_RECORDS_TABLE_DDL,
    _IDX_INGEST_STATE_DDL,
    _IDX_INGEST_SESSION_DDL,
    _CLAIMS_TABLE_DDL,
    _OWNER_REFS_TABLE_DDL,
    _FORK_CLAIMS_TABLE_DDL,
    _FORK_CLAIMS_INDEX_DDL,
    _FORK_MAPPINGS_TABLE_DDL,
    _FORK_MAPPINGS_INDEX_DDL,
    *_REVISION_DDL,
    _IDX_OWNER_REFS_BLOB_DDL,
    _IDX_OWNER_REFS_SESSION_DDL,
)

_REQUIRED_TABLES = (
    "attachment_blobs",
    "attachment_ingest_records",
    "attachment_blob_commit_claims",
    "attachment_owner_refs",
    "fork_attachment_claims",
    "fork_attachment_mappings",
    "attachment_catalog_revision",
)
