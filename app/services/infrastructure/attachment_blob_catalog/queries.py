"""引用感知 tombstone / GC 这条垂直链路（零引用 + retention 后先提交再删）。

承载 attachment_blobs 的 GC 候选枚举、原子 tombstone 提交与已 tombstone
枚举。宿主为 AttachmentBlobCatalog；本链路绝不扫盘。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    validate_blob_id,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    AttachmentBlobRecord,
    BlobCommitClaim,
    _blob_from_row,
    _claim_from_row,
)


class AttachmentBlobRetainedError(RuntimeError):
    """GC 候选在事务重查时仍有依赖，由下一次回收重新判断。"""


_UNPUBLISHED_GC_PREDICATE = (
    "c.state IN ('claimed', 'aborted') "
    "AND NOT EXISTS (SELECT 1 FROM attachment_blobs b WHERE b.blob_id=c.blob_id) "
    "AND NOT EXISTS (SELECT 1 FROM attachment_ingest_records i WHERE i.blob_id=c.blob_id "
    "AND i.state NOT IN ('published', 'aborted')) "
    "AND NOT EXISTS (SELECT 1 FROM fork_attachment_claims f WHERE f.blob_id=c.blob_id "
    "AND f.state IN ('preparing', 'owner_reserved')) "
    "AND NOT EXISTS (SELECT 1 FROM attachment_owner_refs r WHERE r.blob_id=c.blob_id)"
)


class QueriesMixin:
    """引用感知 tombstone / GC 方法族（唯一实现点）。"""


    # ------------------------------------------------------------------
    # 引用感知 tombstone / GC（零引用 + retention 后先提交 tombstone 再删正文）
    # ------------------------------------------------------------------

    def list_gc_candidates(self, *, before: datetime) -> tuple[AttachmentBlobRecord, ...]:
        """枚举零 active 引用且早于 retention 的 available blob（不扫盘）。"""
        if not isinstance(before, datetime):
            raise TypeError(f"before 必须是 datetime: {before!r}")
        if before.tzinfo is None:
            raise ValueError(f"before 必须带时区: {before!r}")
        connection = self._connected()
        invalid = connection.execute(
            "SELECT owner_ref_id, retention_until FROM attachment_owner_refs "
            "WHERE retention_until IS NOT NULL AND julianday(retention_until) IS NULL LIMIT 1"
        ).fetchone()
        if invalid is not None:
            raise RuntimeError(
                "attachment owner retention_until 损坏，拒绝 GC: "
                f"owner_ref_id={invalid['owner_ref_id']}, retention_until={invalid['retention_until']!r}"
            )
        rows = connection.execute(
            "SELECT b.* FROM attachment_blobs AS b "
            "WHERE b.availability = 'available' AND b.created_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM attachment_owner_refs AS r "
            "  WHERE r.blob_id = b.blob_id AND (r.state = 'active' "
            "    OR julianday(r.retention_until) > julianday(?))) "
            "AND NOT EXISTS (SELECT 1 FROM fork_attachment_claims AS c "
            "  WHERE c.blob_id = b.blob_id AND c.state IN ('preparing', 'owner_reserved')) "
            "AND NOT EXISTS (SELECT 1 FROM attachment_ingest_records AS i "
            "  WHERE i.blob_id = b.blob_id AND i.state NOT IN ('published', 'aborted')) "
            "ORDER BY b.created_at, b.rowid",
            (before.astimezone(UTC).isoformat(), datetime.now(UTC).isoformat()),
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
            retained = connection.execute(
                "SELECT 1 FROM attachment_owner_refs WHERE blob_id = ? AND (state = 'active' "
                "OR (retention_until IS NOT NULL AND (julianday(retention_until) IS NULL "
                "OR julianday(retention_until) > julianday(?)))) "
                "UNION ALL SELECT 1 FROM fork_attachment_claims WHERE blob_id = ? "
                "AND state IN ('preparing', 'owner_reserved') "
                "UNION ALL SELECT 1 FROM attachment_ingest_records WHERE blob_id = ? "
                "AND state NOT IN ('published', 'aborted') LIMIT 1", (blob_id, now_text, blob_id, blob_id),
            ).fetchone()
            if retained is not None:
                raise AttachmentBlobRetainedError("attachment blob 仍有 owner retention、copy claim 或 ingest，拒绝 GC tombstone")
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

    def list_unpublished_gc_claims(self, *, before: datetime) -> tuple[BlobCommitClaim, ...]:
        """仅枚举没有发布或操作依赖的冻结 claim；aborted 覆盖删除中途崩溃。"""
        rows = self._connected().execute(
            "SELECT c.* FROM attachment_blob_commit_claims c WHERE " + _UNPUBLISHED_GC_PREDICATE
            + " AND c.created_at < ? ORDER BY c.created_at, c.rowid", (before.astimezone(UTC).isoformat(),),
        ).fetchall()
        return tuple(_claim_from_row(row) for row in rows)

    def abort_unused_blob_claim(self, blob_id: str) -> BlobCommitClaim | None:
        """正文锁内重查后持久终止无使用 claim；保留首次 locator 供新上传复用。"""
        validate_blob_id(blob_id)
        with self.write_transaction() as connection:
            row = connection.execute(
                "SELECT c.* FROM attachment_blob_commit_claims c WHERE c.blob_id=? AND "
                + _UNPUBLISHED_GC_PREDICATE, (blob_id,),
            ).fetchone()
            if row is None:
                return None
            if row["state"] == "claimed":
                connection.execute(
                    "UPDATE attachment_blob_commit_claims SET state='aborted', updated_at=? WHERE blob_id=?",
                    (datetime.now(UTC).isoformat(), blob_id),
                )
                row = connection.execute("SELECT * FROM attachment_blob_commit_claims WHERE blob_id=?", (blob_id,)).fetchone()
            return _claim_from_row(row)
