"""blob availability 与 owner reference 这条垂直链路（可见性提交）。

承载 publish_blob_and_owner_ref 这一附件可见性的唯一提交点，以及
blob/owner_ref 的读取与 session 级释放。宿主为 AttachmentBlobCatalog。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    BlobIdentity,
    BlobIdentityConflictError,
    date_bucket_relative_locator,
    validate_blob_id,
    validate_blob_relative_locator,
    validate_digest,
)
from app.services.infrastructure.attachment_blob_catalog.ingest import (
    _update_ingest_record_identity,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    AttachmentBlobRecord,
    AttachmentOwnerRef,
    _blob_from_row,
    _owner_ref_from_row,
)
from app.services.infrastructure.attachment_blob_catalog.schema import (
    _date_from_locator,
    _validate_non_empty,
)


def _publish_blob_metadata(
    connection: sqlite3.Connection, *, identity: BlobIdentity, relative_locator: str,
    mime_type: str | None, protection: str, now_text: str, restore_available: bool = False,
) -> None:
    """正文 catalog 的唯一写入点；导入由实际校验过的正文和精确 fork claim 保活。"""
    validate_digest(identity.digest)
    validate_blob_id(identity.blob_id)
    validate_blob_relative_locator(relative_locator)
    if relative_locator != date_bucket_relative_locator(identity.blob_id, _date_from_locator(relative_locator)):
        raise ValueError(f"relative locator 与 blob-id 不一致: locator={relative_locator!r}, blob_id={identity.blob_id!r}")
    row = connection.execute("SELECT * FROM attachment_blobs WHERE blob_id = ?", (identity.blob_id,)).fetchone()
    if row is None:
        conflict = connection.execute("SELECT blob_id, length FROM attachment_blobs WHERE digest = ?", (identity.digest,)).fetchone()
        if conflict is not None:
            raise BlobIdentityConflictError(
                "同一 digest 已绑定不同 blob-id（blob-identity-conflict）: "
                f"digest={identity.digest!r}, existing_blob_id={conflict['blob_id']!r}, actual_blob_id={identity.blob_id!r}"
            )
        connection.execute(
            "INSERT INTO attachment_blobs (blob_id, digest, relative_locator, length, mime_type, protection, availability, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'available', ?, ?)",
            (identity.blob_id, identity.digest, relative_locator, identity.length, mime_type, protection, now_text, now_text),
        )
        return
    blob = _blob_from_row(row)
    if blob.digest != identity.digest or blob.length != identity.length:
        raise BlobIdentityConflictError(
            "同一 blob-id 对应不同 digest/length（blob-identity-conflict）: "
            f"blob_id={identity.blob_id!r}, existing_digest={blob.digest!r}, actual_digest={identity.digest!r}, "
            f"existing_length={blob.length}, actual_length={identity.length}"
        )
    if blob.relative_locator != relative_locator:
        raise BlobIdentityConflictError(
            "blob 已发布的 relative locator 与本次不一致（不得按新日期复制 blob，复用首次 locator）: "
            f"blob_id={identity.blob_id!r}, existing={blob.relative_locator!r}, actual={relative_locator!r}"
        )
    if restore_available and blob.availability != "available":
        connection.execute("UPDATE attachment_blobs SET availability='available', updated_at=? WHERE blob_id=?", (now_text, identity.blob_id))


class BlobsMixin:
    """blob availability 与 owner reference 方法族（唯一实现点）。"""


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
        if retention_until is not None:
            deadline = datetime.fromisoformat(retention_until)
            if deadline.tzinfo is None:
                raise ValueError("attachment retention_until 必须带时区")
            retention_until = deadline.astimezone(UTC).isoformat()
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
            _publish_blob_metadata(connection, identity=identity, relative_locator=relative_locator,
                                   mime_type=mime_type, protection=protection, now_text=now_text)

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
                    or ref.retention_until != retention_until
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

            _update_ingest_record_identity(  # 单点：与 ingest 共用同一条状态迁移 UPDATE
                connection,
                ingest_idempotency_key=ingest_idempotency_key,
                identity=identity,
                now_text=now_text,
                state="published",
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

    def get_reusable_attachment_owner(self, attachment_id: str) -> AttachmentOwnerRef | None:
        """只复用已发布且仍 active 的引用；预留引用不授予第二次 fork。"""
        _validate_non_empty("attachment_id", attachment_id)
        row = self._connected().execute(
            "SELECT owner.* FROM attachment_owner_refs owner "
            "JOIN attachment_blobs blob ON blob.blob_id=owner.blob_id "
            "WHERE owner.attachment_id=? AND owner.state='active' AND blob.availability='available' "
            "AND NOT EXISTS (SELECT 1 FROM fork_attachment_claims claim "
            "WHERE claim.target_owner_ref_id=owner.owner_ref_id AND claim.state IN ('preparing','owner_reserved'))",
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
        self, *, owner_session_id: str, reason: str, owner_thread_id: str | None = None,
    ) -> tuple[AttachmentOwnerRef, ...]:
        """释放该 session 的全部 active owner reference（删除 drain 定点调用）。"""
        _validate_non_empty("owner_session_id", owner_session_id)
        _validate_non_empty("reason", reason)
        if owner_thread_id is not None:
            _validate_non_empty("owner_thread_id", owner_thread_id)
        now_text = datetime.now(UTC).isoformat()
        with self.write_transaction() as connection:
            rows = connection.execute(
                "SELECT owner_ref_id FROM attachment_owner_refs "
                "WHERE owner_session_id = ? AND state = 'active' AND (? IS NULL OR owner_thread_id=?)",
                (owner_session_id, owner_thread_id, owner_thread_id),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE attachment_owner_refs SET state = 'released', "
                    "released_reason = ?, updated_at = ? WHERE owner_ref_id = ?",
                    (reason, now_text, str(row["owner_ref_id"])),
                )
            released = connection.execute(
                "SELECT * FROM attachment_owner_refs WHERE owner_session_id = ? AND (? IS NULL OR owner_thread_id=?) "
                "ORDER BY created_at, rowid",
                (owner_session_id, owner_thread_id, owner_thread_id),
            ).fetchall()
            return tuple(_owner_ref_from_row(row) for row in released)
