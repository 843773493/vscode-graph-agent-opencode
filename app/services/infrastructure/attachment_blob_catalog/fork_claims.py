"""附件 catalog 内的 copy claim 与 target owner reservation，沿原 SQLite owner。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime

from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    BlobIdentity,
)
from app.services.infrastructure.attachment_blob_catalog.blobs import (
    _publish_blob_metadata,
)
from app.services.infrastructure.attachment_blob_catalog.ingest import (
    _claim_blob_identity,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    AttachmentBlobRecord,
    AttachmentOwnerRef,
    ForkAttachmentSnapshot,
    _blob_from_row,
    _owner_ref_from_row,
)


def _claim_snapshot(row: sqlite3.Row) -> ForkAttachmentSnapshot:
    snapshot = ForkAttachmentSnapshot(**json.loads(row["snapshot_json"]))
    digest = hashlib.sha256((row["copy_operation_id"] + row["source_snapshot_id"] + row["operation_preimage_hash"] + row["snapshot_json"]).encode()).hexdigest()
    if digest != row["preimage_hash"] or (
        snapshot.claim_id, snapshot.source_attachment_id, snapshot.target_owner_ref_id,
        snapshot.target_session_id, snapshot.target_thread_id, snapshot.blob_id,
    ) != (
        row["claim_id"], row["source_attachment_id"], row["target_owner_ref_id"],
        row["target_session_id"], row["target_thread_id"], row["blob_id"],
    ):
        raise RuntimeError("fork attachment claim frozen preimage/hash 已损坏")
    return snapshot


class ForkAttachmentClaimsMixin:
    """同一 copy operation 的 claim 集合只允许原子冻结、保留、终结。"""

    def freeze_fork_attachment_sources(self, attachment_ids: tuple[str, ...]) -> tuple[int, tuple[tuple[AttachmentOwnerRef, AttachmentBlobRecord], ...]]:
        connection = self._connected()
        connection.execute("BEGIN")
        try:
            revision = connection.execute("SELECT revision FROM attachment_catalog_revision WHERE id = 1").fetchone()[0]
            records = []
            for attachment_id in attachment_ids:
                source = connection.execute("SELECT * FROM attachment_owner_refs WHERE attachment_id = ?", (attachment_id,)).fetchone()
                if source is None:
                    raise RuntimeError("source-mismatch: fork logical attachment 缺少 owner ref")
                blob = connection.execute("SELECT * FROM attachment_blobs WHERE blob_id = ?", (source["blob_id"],)).fetchone()
                if blob is None:
                    raise RuntimeError("source-mismatch: fork owner ref 缺少 blob catalog 行")
                records.append((_owner_ref_from_row(source), _blob_from_row(blob)))
        finally:
            connection.rollback()
        return revision, tuple(records)

    def prepare_fork_attachment_claims(
        self, *, copy_operation_id: str, source_snapshot_id: str,
        operation_preimage_hash: str,
        snapshots: Sequence[ForkAttachmentSnapshot],
        imported_locators: Mapping[str, str | None] | None = None,
    ) -> None:
        """来源冻结本地 owner；目标凭导出回执先建立 claim，再原子登记已校验正文。"""
        with self.write_transaction() as connection:
            for snapshot in snapshots:
                if imported_locators is not None and snapshot.availability == "available":
                    if snapshot.target_action != "copy_snapshot" or snapshot.claim_id is None or snapshot.blob_id not in imported_locators:
                        raise RuntimeError("fork imported 附件必须绑定冻结 copy_snapshot claim")
                    locator = imported_locators[snapshot.blob_id]
                    if locator is not None:
                        _publish_blob_metadata(
                            connection, identity=BlobIdentity(snapshot.blob_id, snapshot.digest, snapshot.length),
                            relative_locator=locator, mime_type=snapshot.mime_type, protection="private",
                            now_text=datetime.now(UTC).isoformat(), restore_available=True,
                        )
                        connection.execute(
                            "UPDATE attachment_blob_commit_claims SET state='published', updated_at=? WHERE blob_id=? AND state='claimed'",
                            (datetime.now(UTC).isoformat(), snapshot.blob_id),
                        )
                encoded = json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                target_file_id = f"boxteam-session://{snapshot.target_session_id}/attachments/{snapshot.target_attachment_id}"
                for alias in dict.fromkeys((snapshot.source_file_id, *snapshot.source_file_aliases, target_file_id)):
                    mapping = connection.execute(
                        "SELECT snapshot_json FROM fork_attachment_mappings WHERE copy_operation_id=? AND source_file_id=?",
                        (copy_operation_id, alias),
                    ).fetchone()
                    if mapping is not None and mapping[0] != encoded:
                        raise RuntimeError("fork attachment mapping 冻结前像冲突")
                    connection.execute(
                        "INSERT INTO fork_attachment_mappings VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        (copy_operation_id, alias, snapshot.target_session_id, snapshot.target_thread_id, encoded),
                    )
                if snapshot.claim_id is None:
                    if snapshot.availability == "available":
                        owner = self.get_reusable_attachment_owner(snapshot.target_attachment_id)
                        blob = connection.execute("SELECT * FROM attachment_blobs WHERE blob_id=?", (snapshot.blob_id,)).fetchone()
                        if snapshot.target_action != "reuse_reference" or owner is None or blob is None or (
                            owner.owner_ref_id, owner.owner_session_id, owner.owner_thread_id,
                            owner.blob_id, owner.digest, owner.variant, owner.item_ref,
                            blob["digest"], blob["length"], blob["relative_locator"],
                        ) != (
                            snapshot.target_owner_ref_id, snapshot.target_session_id, snapshot.target_owner_thread_id,
                            snapshot.blob_id, snapshot.digest, snapshot.variant, snapshot.target_item_ref,
                            snapshot.digest, snapshot.length, snapshot.relative_locator,
                        ):
                            raise RuntimeError("fork attachment claimless reuse 缺少精确已发布 owner/blob")
                    elif snapshot.required:
                        raise RuntimeError("required fork attachment 不可用")
                    continue
                preimage = hashlib.sha256((copy_operation_id + source_snapshot_id + operation_preimage_hash + encoded).encode()).hexdigest()
                row = connection.execute("SELECT * FROM fork_attachment_claims WHERE claim_id = ?", (snapshot.claim_id,)).fetchone()
                if row is not None:
                    if (row["copy_operation_id"], row["source_snapshot_id"], row["snapshot_json"], row["preimage_hash"]) != (
                        copy_operation_id, source_snapshot_id, encoded, preimage,
                    ) or row["state"] in {"aborted", "target_deleted"}:
                        raise RuntimeError("fork attachment claim 同 key 不同 preimage 或已终结")
                    continue
                if imported_locators is None:
                    source = connection.execute("SELECT * FROM attachment_owner_refs WHERE owner_ref_id = ?", (snapshot.source_owner_ref_id,)).fetchone()
                    blob = connection.execute("SELECT * FROM attachment_blobs WHERE blob_id = ?", (snapshot.blob_id,)).fetchone()
                    if source is None or blob is None or (
                        source["attachment_id"], source["owner_session_id"], source["owner_thread_id"], source["state"],
                        source["blob_id"], source["digest"], source["variant"], source["item_ref"],
                        blob["availability"], blob["digest"], blob["length"], blob["relative_locator"],
                    ) != (
                        snapshot.source_attachment_id, snapshot.source_session_id, snapshot.source_thread_id, "active",
                        snapshot.blob_id, snapshot.digest, snapshot.variant, snapshot.source_item_ref,
                        "available", snapshot.digest, snapshot.length, snapshot.relative_locator,
                    ):
                        raise RuntimeError("source-mismatch: fork attachment owner/blob 冻结前像已变化")
                connection.execute(
                    "INSERT INTO fork_attachment_claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'preparing')",
                    (snapshot.claim_id, copy_operation_id, source_snapshot_id, snapshot.source_attachment_id,
                     snapshot.target_owner_ref_id, snapshot.target_session_id, snapshot.target_thread_id,
                     snapshot.blob_id, encoded, operation_preimage_hash, preimage),
                )

    def reserve_fork_blob_import(self, copy_operation_id: str, snapshot: ForkAttachmentSnapshot) -> tuple[str, str]:
        """正文 staging 属于本 operation；最终 locator 仍由唯一 digest claim 决定。"""
        ingest_id = "ing_" + hashlib.sha256(f"fork-import:{copy_operation_id}:{snapshot.blob_id}".encode()).hexdigest()[:32]
        with self.write_transaction() as connection:
            row = connection.execute("SELECT * FROM attachment_blobs WHERE blob_id=?", (snapshot.blob_id,)).fetchone()
            if row is not None:
                blob = _blob_from_row(row)
                if (blob.digest, blob.length) != (snapshot.digest, snapshot.length):
                    raise RuntimeError("fork imported blob 与目标已有 digest/length 冲突")
                return ingest_id, blob.relative_locator
            claim = _claim_blob_identity(connection, identity=BlobIdentity(snapshot.blob_id, snapshot.digest, snapshot.length),
                                         winning_ingest_id=ingest_id, now=datetime.now(UTC))
            if claim.state == "aborted":
                raise RuntimeError("fork imported blob claim 已 aborted，拒绝复活")
            return ingest_id, claim.final_relative_locator

    def release_fork_export_claims(self, copy_operation_id: str) -> None:
        """来源导出从未预留外部 target owner；仅移除本导出的保活和 alias。"""
        with self.write_transaction() as connection:
            rows = connection.execute("SELECT * FROM fork_attachment_claims WHERE copy_operation_id=?", (copy_operation_id,)).fetchall()
            for row in rows:
                _claim_snapshot(row)
                if row["state"] != "preparing":
                    raise RuntimeError("fork export 释放发现目标 owner reservation，拒绝混用本地 settlement")
            connection.execute("DELETE FROM fork_attachment_claims WHERE copy_operation_id=?", (copy_operation_id,))
            connection.execute("DELETE FROM fork_attachment_mappings WHERE copy_operation_id=?", (copy_operation_id,))

    def reserve_fork_attachment_owners(self, *, copy_operation_id: str, claim_ids: tuple[str, ...]) -> None:
        """全量 required claim 达到 owner_reserved 后才允许 target publication。"""
        now = datetime.now(UTC).isoformat()
        with self.write_transaction() as connection:
            rows = connection.execute("SELECT * FROM fork_attachment_claims WHERE copy_operation_id = ? ORDER BY claim_id", (copy_operation_id,)).fetchall()
            if tuple(row["claim_id"] for row in rows) != claim_ids:
                raise RuntimeError("fork attachment claim 集合与 captured marker 不一致")
            mappings = connection.execute(
                "SELECT DISTINCT snapshot_json FROM fork_attachment_mappings WHERE copy_operation_id=?",
                (copy_operation_id,),
            ).fetchall()
            for mapping in mappings:
                snapshot = ForkAttachmentSnapshot(**json.loads(mapping[0]))
                if snapshot.availability == "available" and snapshot.claim_id is None:
                    owner = self.get_reusable_attachment_owner(snapshot.target_attachment_id)
                    if owner is None or (owner.owner_ref_id, owner.owner_session_id, owner.owner_thread_id,
                                         owner.blob_id, owner.digest, owner.variant, owner.item_ref) != (
                        snapshot.target_owner_ref_id, snapshot.target_session_id, snapshot.target_owner_thread_id,
                        snapshot.blob_id, snapshot.digest, snapshot.variant, snapshot.target_item_ref,
                    ):
                        raise RuntimeError("fork attachment claimless owner 在 target publication 前已变化")
            for row in rows:
                if row["state"] in {"aborted", "target_deleted"}:
                    raise RuntimeError("fork attachment claim 已终结，不得重建 owner")
                snapshot = _claim_snapshot(row)
                blob = connection.execute("SELECT * FROM attachment_blobs WHERE blob_id = ?", (snapshot.blob_id,)).fetchone()
                if blob is None or (blob["availability"], blob["digest"], blob["length"]) != (
                    "available", snapshot.digest, snapshot.length,
                ):
                    raise RuntimeError("fork attachment reservation blob 已变化")
                existing = connection.execute("SELECT * FROM attachment_owner_refs WHERE owner_ref_id = ?", (snapshot.target_owner_ref_id,)).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO attachment_owner_refs (owner_ref_id, attachment_id, blob_id, digest, owner_session_id, "
                        "owner_thread_id, file_name, mime_type, variant, item_ref, state, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                        (snapshot.target_owner_ref_id, snapshot.target_attachment_id, snapshot.blob_id, snapshot.digest,
                          snapshot.target_session_id, snapshot.target_owner_thread_id, snapshot.file_name, snapshot.mime_type,
                         snapshot.variant, snapshot.target_item_ref, now, now),
                    )
                elif (existing["attachment_id"], existing["owner_session_id"], existing["owner_thread_id"], existing["blob_id"], existing["digest"], existing["state"]) != (
                    snapshot.target_attachment_id, snapshot.target_session_id, snapshot.target_owner_thread_id,
                    snapshot.blob_id, snapshot.digest, "active",
                ):
                    raise RuntimeError("fork attachment owner reservation 冲突，拒绝复活 released reference")
                if row["state"] == "preparing":
                    connection.execute("UPDATE fork_attachment_claims SET state = 'owner_reserved' WHERE claim_id = ? AND state = 'preparing'", (row["claim_id"],))

    def settle_fork_attachment_claims(self, *, copy_operation_id: str, claim_ids: tuple[str, ...], state: str) -> None:
        if state not in {"committed", "aborted", "target_deleted"}:
            raise ValueError("fork attachment claim 终态非法")
        with self.write_transaction() as connection:
            rows = connection.execute("SELECT * FROM fork_attachment_claims WHERE copy_operation_id = ? ORDER BY claim_id", (copy_operation_id,)).fetchall()
            if tuple(row["claim_id"] for row in rows) != claim_ids:
                raise RuntimeError("fork attachment settlement claim 集合不一致")
            for row in rows:
                snapshot = _claim_snapshot(row)
                current = row["state"]
                if current == state:
                    continue
                if current in {"aborted", "target_deleted"} or (state == "committed" and current != "owner_reserved") or (state == "aborted" and current == "committed"):
                    raise RuntimeError("fork attachment claim 终态 CAS 冲突")
                owner = connection.execute("SELECT * FROM attachment_owner_refs WHERE owner_ref_id = ?", (snapshot.target_owner_ref_id,)).fetchone()
                if owner is None:
                    if current != "preparing":
                        raise RuntimeError("fork attachment reserved/committed claim 缺少 target owner ref")
                elif (owner["attachment_id"], owner["blob_id"], owner["digest"], owner["owner_session_id"], owner["owner_thread_id"], owner["variant"], owner["item_ref"]) != (
                    snapshot.target_attachment_id, snapshot.blob_id, snapshot.digest,
                    snapshot.target_session_id, snapshot.target_owner_thread_id, snapshot.variant, snapshot.target_item_ref,
                ):
                    raise RuntimeError("fork attachment settlement target owner ref 与冻结 claim 不一致")
                if state == "committed" and (owner is None or owner["state"] != "active"):
                    raise RuntimeError("fork attachment committed claim 必须有 active target owner ref")
                if state != "committed":
                    connection.execute("UPDATE attachment_owner_refs SET state = 'released', released_reason = ?, updated_at = ? WHERE owner_ref_id = ? AND state = 'active'", (state, datetime.now(UTC).isoformat(), row["target_owner_ref_id"]))
                connection.execute("UPDATE fork_attachment_claims SET state = ? WHERE claim_id = ?", (state, row["claim_id"]))

    def fork_attachment_claim_ids(self, copy_operation_id: str) -> tuple[str, ...]:
        rows = self._connected().execute("SELECT claim_id FROM fork_attachment_claims WHERE copy_operation_id = ? ORDER BY claim_id", (copy_operation_id,)).fetchall()
        return tuple(row[0] for row in rows)

    def fork_attachment_snapshots(self, copy_operation_id: str, *, target_session_id: str, target_thread_id: str) -> tuple[ForkAttachmentSnapshot, ...]:
        rows = self._connected().execute(
            "SELECT source_file_id,snapshot_json FROM fork_attachment_mappings "
            "WHERE copy_operation_id=? AND target_session_id=? AND target_thread_id=? ORDER BY source_file_id",
            (copy_operation_id, target_session_id, target_thread_id),
        ).fetchall()
        snapshots = []
        for alias, encoded in rows:
            snapshot = ForkAttachmentSnapshot(**json.loads(encoded))
            if alias == snapshot.source_file_id:
                if (snapshot.target_session_id, snapshot.target_thread_id) != (target_session_id, target_thread_id):
                    raise RuntimeError("fork attachment mapping 与目标 owner 不一致")
                snapshots.append(snapshot)
        return tuple(snapshots)

    def fork_attachment_operations_for_target(self, target_session_id: str, *, target_thread_id: str | None = None) -> tuple[str, ...]:
        rows = self._connected().execute(
            "SELECT DISTINCT copy_operation_id FROM fork_attachment_claims WHERE target_session_id=? "
            "AND (? IS NULL OR target_thread_id=?) AND state NOT IN ('aborted','target_deleted') ORDER BY copy_operation_id",
            (target_session_id, target_thread_id, target_thread_id),
        ).fetchall()
        return tuple(row[0] for row in rows)

    def fork_attachment_snapshot_for_alias(
        self, *, target_session_id: str, target_thread_id: str, source_file_id: str,
    ) -> ForkAttachmentSnapshot | None:
        """读取 target 本地冻结事实；不可用附件也不回读更早来源。"""
        rows = self._connected().execute(
            "SELECT snapshot_json FROM fork_attachment_mappings "
            "WHERE target_session_id=? AND target_thread_id=? AND source_file_id=? LIMIT 2",
            (target_session_id, target_thread_id, source_file_id),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("fork attachment target-local alias 歧义")
        if not rows:
            return None
        snapshot = ForkAttachmentSnapshot(**json.loads(rows[0][0]))
        if (snapshot.target_session_id, snapshot.target_thread_id) != (target_session_id, target_thread_id):
            raise RuntimeError("fork attachment 冻结 mapping 不属于指定 Thread")
        return snapshot

    def project_fork_attachment(
        self, *, target_session_id: str, target_thread_id: str, source_file_id: str,
    ) -> dict[str, str]:
        """只投影精确目标 Thread 的冻结 alias，并核当前 owner ref 是否仍保活。"""
        connection = self._connected()
        snapshot = self.fork_attachment_snapshot_for_alias(
            target_session_id=target_session_id, target_thread_id=target_thread_id, source_file_id=source_file_id,
        )
        if snapshot is not None:
            owner = connection.execute(
                "SELECT owner.state,blob.availability,owner.attachment_id,owner.blob_id,owner.digest,"
                "owner.variant,owner.item_ref,blob.digest,blob.length FROM attachment_owner_refs owner "
                "JOIN attachment_blobs blob ON blob.blob_id=owner.blob_id "
                "WHERE owner.owner_ref_id=? AND owner.owner_session_id=? AND owner.owner_thread_id=?",
                (snapshot.target_owner_ref_id, target_session_id, snapshot.target_owner_thread_id),
            ).fetchone()
            published = snapshot.claim_id is None or connection.execute(
                "SELECT 1 FROM fork_attachment_claims WHERE claim_id=? AND target_owner_ref_id=? AND state='committed'",
                (snapshot.claim_id, snapshot.target_owner_ref_id),
            ).fetchone() is not None
            return {
                "file_id": f"boxteam-session://{target_session_id}/attachments/{snapshot.target_attachment_id}",
                "availability": "available" if snapshot.availability == "available" and published and owner is not None and tuple(owner) == (
                    "active", "available", snapshot.target_attachment_id, snapshot.blob_id, snapshot.digest,
                    snapshot.variant, snapshot.target_item_ref, snapshot.digest, snapshot.length,
                ) else "unavailable",
            }
        owner = connection.execute(
            "SELECT owner.state,blob.availability FROM attachment_owner_refs owner "
            "JOIN attachment_blobs blob ON blob.blob_id=owner.blob_id "
            "WHERE owner.owner_session_id=? AND owner.owner_thread_id=? "
            "AND ?='boxteam-session://' || owner.owner_session_id || '/attachments/' || owner.attachment_id "
            "AND NOT EXISTS (SELECT 1 FROM fork_attachment_claims claim "
            "WHERE claim.target_owner_ref_id=owner.owner_ref_id AND claim.state IN ('preparing','owner_reserved'))",
            (target_session_id, target_thread_id, source_file_id),
        ).fetchone()
        return {"file_id": source_file_id,
                "availability": "available" if owner is not None and tuple(owner) == ("active", "available") else "unavailable"}
