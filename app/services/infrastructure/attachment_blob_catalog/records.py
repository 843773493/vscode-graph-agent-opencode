"""attachment blob catalog 的不可变行投影与逻辑 attachment_id 派生。

四个 dataclass(frozen=True, slots=True) 精确对应四张表的行形态；四个
_from_row 函数是这些投影的唯一构造点（逐字段 str()/int() 归一，可空列还原
为 None）。derive_attachment_id 只由 blob 身份与 owner 派生。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    validate_blob_id,
)

__all__ = [
    "AttachmentBlobRecord",
    "AttachmentIngestRecord",
    "AttachmentOwnerRef",
    "BlobCommitClaim",
    "derive_attachment_id",
]



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
