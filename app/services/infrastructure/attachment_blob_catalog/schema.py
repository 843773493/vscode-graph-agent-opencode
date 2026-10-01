"""attachment blob catalog 的表 DDL、共享常量与形态校验（唯一 DDL 定义点）。

四张表 DDL、索引 DDL、必需表清单、库文件名与 ingest 终态闭集都在本模块单点
定义；records/ingest/blobs/queries 各 mixin 从这里取用，不复制。

本模块同时承载两个只做形态归一/解析的模块级小工具
（_validate_non_empty、_date_from_locator）；它们被 ingest/blobs 两个 mixin
共用，放在此处避免在多个模块复制同一份实现。
"""

from __future__ import annotations

__all__ = [
    "CATALOG_DATABASE_NAME",
    "INGEST_RECORD_TERMINAL_STATES",
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


def _validate_non_empty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} 必须是非空字符串: {value!r}")


def _date_from_locator(relative_locator: str):
    """从受检 locator 取日期部分（形态已由 validate 保证）。"""
    year_text, month_text, day_text, _blob = relative_locator.split("/")
    from datetime import date as _date

    return _date(int(year_text), int(month_text), int(day_text))
