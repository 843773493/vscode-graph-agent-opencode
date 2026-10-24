"""目录 fork 的冻结载荷、保留与关联结算；不维护第二份历史正文。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.attachment_blob_catalog.records import (
    ForkAttachmentDirectorySnapshot,
    attachment_snapshot_from_payload,
)


def _encode(snapshot: ForkAttachmentDirectorySnapshot) -> str:
    return canonical_json_bytes(snapshot.to_payload()).decode()


def _claim_snapshot(row) -> ForkAttachmentDirectorySnapshot:
    snapshot = attachment_snapshot_from_payload(json.loads(row["snapshot_json"]))
    if not isinstance(snapshot, ForkAttachmentDirectorySnapshot):
        raise TypeError("新目录 claim 不能写入旧 blob 快照")
    preimage = hashlib.sha256(canonical_json_bytes([
        row["copy_operation_id"], row["source_snapshot_id"], row["operation_preimage_hash"], snapshot.to_payload(),
    ])).hexdigest()
    if preimage != row["preimage_hash"] or (
        snapshot.claim_id, snapshot.source_attachment_id, snapshot.target_attachment_id,
        snapshot.target_session_id, snapshot.target_thread_id,
    ) != (row["claim_id"], row["source_attachment_id"], row["target_attachment_id"], row["target_session_id"], row["target_thread_id"]):
        raise RuntimeError("目录 fork claim 的原文、身份或 preimage 已损坏")
    return snapshot


class ForkAttachmentClaimsMixin:
    def prepare_fork_attachment_claims(self, *, copy_operation_id: str, source_snapshot_id: str, operation_preimage_hash: str, snapshots: Sequence[ForkAttachmentDirectorySnapshot]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.write_transaction() as connection:
            for snapshot in snapshots:
                encoded = _encode(snapshot)
                if snapshot.claim_id is not None:
                    existing = connection.execute("SELECT * FROM attachment_directory_fork_claims WHERE claim_id=?", (snapshot.claim_id,)).fetchone()
                    preimage = hashlib.sha256(canonical_json_bytes([copy_operation_id, source_snapshot_id, operation_preimage_hash, snapshot.to_payload()])).hexdigest()
                    if existing is not None:
                        if _claim_snapshot(existing) != snapshot or (existing["copy_operation_id"], existing["source_snapshot_id"], existing["operation_preimage_hash"]) != (copy_operation_id, source_snapshot_id, operation_preimage_hash):
                            raise RuntimeError("同一目录 fork claim 的冻结输入冲突")
                        if existing["state"] in {"aborted", "target_deleted"}:
                            raise RuntimeError("目录 fork claim 已终止，不能重新准备")
                    else:
                        connection.execute(
                            "INSERT INTO attachment_directory_fork_claims (claim_id,copy_operation_id,source_snapshot_id,operation_preimage_hash,preimage_hash,"
                            "source_attachment_id,target_attachment_id,target_session_id,target_thread_id,snapshot_json,state,created_at,updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?, 'prepared',?,?)",
                            (snapshot.claim_id, copy_operation_id, source_snapshot_id, operation_preimage_hash, preimage,
                             snapshot.source_attachment_id, snapshot.target_attachment_id, snapshot.target_session_id,
                             snapshot.target_thread_id, encoded, now, now),
                        )
                for alias in {snapshot.source_file_id, *snapshot.source_file_aliases, snapshot.target_file_id} - {""}:
                    identity = (copy_operation_id, alias, snapshot.target_session_id, snapshot.target_thread_id, encoded)
                    existing = connection.execute(
                        "SELECT * FROM attachment_directory_fork_aliases WHERE (copy_operation_id=? AND source_file_id=?) "
                        "OR (target_session_id=? AND target_thread_id=? AND source_file_id=?)",
                        (copy_operation_id, alias, snapshot.target_session_id, snapshot.target_thread_id, alias),
                    ).fetchall()
                    if existing:
                        if len(existing) != 1 or tuple(existing[0]) != identity:
                            raise RuntimeError("目录 alias 的 operation、消费 owner 或冻结来源冲突")
                    else:
                        connection.execute("INSERT INTO attachment_directory_fork_aliases VALUES (?,?,?,?,?)", identity)

    def fork_attachment_claim_ids(self, copy_operation_id: str) -> tuple[str, ...]:
        rows = self._connected().execute("SELECT * FROM attachment_directory_fork_claims WHERE copy_operation_id=? ORDER BY claim_id", (copy_operation_id,)).fetchall()
        for row in rows:
            _claim_snapshot(row)
        return tuple(row["claim_id"] for row in rows)

    def fork_attachment_snapshots(self, copy_operation_id: str, *, target_session_id: str, target_thread_id: str) -> tuple[ForkAttachmentDirectorySnapshot, ...]:
        result: dict[str, ForkAttachmentDirectorySnapshot] = {}
        for row in self._connected().execute("SELECT snapshot_json FROM attachment_directory_fork_aliases WHERE copy_operation_id=? AND target_session_id=? AND target_thread_id=?", (copy_operation_id, target_session_id, target_thread_id)):
            snapshot = attachment_snapshot_from_payload(json.loads(row[0]))
            if not isinstance(snapshot, ForkAttachmentDirectorySnapshot):
                raise TypeError("新目录 alias 包含旧 blob 快照")
            previous = result.setdefault(snapshot.source_attachment_id, snapshot)
            if previous != snapshot:
                raise RuntimeError("同一目录 fork 的 alias 冻结输入不一致")
        return tuple(result[key] for key in sorted(result))

    def fork_attachment_snapshot_for_alias(self, *, target_session_id: str, target_thread_id: str, source_file_id: str) -> ForkAttachmentDirectorySnapshot | None:
        row = self._connected().execute(
            "SELECT snapshot_json FROM attachment_directory_fork_aliases WHERE target_session_id=? AND target_thread_id=? AND source_file_id=?",
            (target_session_id, target_thread_id, source_file_id),
        ).fetchone()
        if row is None:
            return None
        snapshot = attachment_snapshot_from_payload(json.loads(row[0]))
        if not isinstance(snapshot, ForkAttachmentDirectorySnapshot) or (snapshot.target_session_id, snapshot.target_thread_id) != (target_session_id, target_thread_id) or source_file_id not in {snapshot.source_file_id, *snapshot.source_file_aliases, snapshot.target_file_id}:
            raise RuntimeError("目录 alias 与精确消费 Thread 不一致")
        if snapshot.claim_id is not None:
            claim = self._connected().execute("SELECT * FROM attachment_directory_fork_claims WHERE claim_id=?", (snapshot.claim_id,)).fetchone()
            if claim is None or _claim_snapshot(claim) != snapshot:
                raise RuntimeError("目录 alias 与原 claim 的冻结内容不一致")
        return snapshot

    def reserve_fork_attachment_owners(self, *, copy_operation_id: str, claim_ids: Sequence[str]) -> None:
        with self.write_transaction() as connection:
            for claim_id in claim_ids:
                row = connection.execute("SELECT * FROM attachment_directory_fork_claims WHERE claim_id=? AND copy_operation_id=?", (claim_id, copy_operation_id)).fetchone()
                if row is None:
                    raise RuntimeError("目录 fork reservation 缺少原 claim")
                snapshot = _claim_snapshot(row)
                self.require_available_directory(snapshot.target_attachment_id)
                if row["state"] in {"aborted", "target_deleted"}:
                    raise RuntimeError("不能预留已终止的目录 fork")
                if row["state"] == "prepared":
                    connection.execute("UPDATE attachment_directory_fork_claims SET state='owner_reserved',updated_at=? WHERE claim_id=?", (datetime.now(UTC).isoformat(), claim_id))

    def settle_fork_attachment_claims(self, *, copy_operation_id: str, claim_ids: Sequence[str], state: str) -> None:
        if state not in {"committed", "aborted", "target_deleted"}:
            raise ValueError("目录 fork 结算状态非法")
        with self.write_transaction() as connection:
            now = datetime.now(UTC)
            for claim_id in claim_ids:
                row = connection.execute("SELECT * FROM attachment_directory_fork_claims WHERE claim_id=? AND copy_operation_id=?", (claim_id, copy_operation_id)).fetchone()
                if row is None:
                    raise RuntimeError("目录 fork 结算缺少原 claim")
                snapshot = _claim_snapshot(row)
                if row["state"] == state:
                    continue
                if row["state"] in {"aborted", "target_deleted"} or (row["state"] == "committed" and state != "target_deleted"):
                    raise RuntimeError("目录 fork 不能改变已结算的终态")
                if state == "committed":
                    if row["state"] != "owner_reserved":
                        raise RuntimeError("目录 fork 尚未完成目标 reservation")
                    self.require_available_directory(snapshot.target_attachment_id)
                    self._attach(connection, snapshot.target_attachment_id, snapshot.target_session_id, snapshot.target_thread_id, "fork:" + copy_operation_id, now)
                else:
                    connection.execute(
                        "UPDATE attachment_directory_associations SET state='released',updated_at=? WHERE attachment_id=? AND session_id=? AND thread_id=? AND binding_token=? AND state='active'",
                        (now.isoformat(), snapshot.target_attachment_id, snapshot.target_session_id, snapshot.target_thread_id, "fork:" + copy_operation_id),
                    )
                    self._mark_zero_associations(connection, snapshot.target_attachment_id, now)
                connection.execute("UPDATE attachment_directory_fork_claims SET state=?,updated_at=? WHERE claim_id=?", (state, now.isoformat(), claim_id))

    def release_fork_export_claims(self, copy_operation_id: str) -> None:
        with self.write_transaction() as connection:
            connection.execute("UPDATE attachment_directory_fork_claims SET export_released=1,updated_at=? WHERE copy_operation_id=?", (datetime.now(UTC).isoformat(), copy_operation_id))

    def fork_attachment_operations_for_target(self, target_session_id: str, *, target_thread_id: str | None = None) -> tuple[str, ...]:
        query = "SELECT DISTINCT copy_operation_id FROM attachment_directory_fork_claims WHERE target_session_id=? AND state NOT IN ('aborted','target_deleted')"
        values = [target_session_id]
        if target_thread_id is not None:
            query += " AND target_thread_id=?"
            values.append(target_thread_id)
        return tuple(row[0] for row in self._connected().execute(query, values))

    def publish_fork_directory(self, connection: sqlite3.Connection, snapshot: ForkAttachmentDirectorySnapshot) -> None:
        """与调用方的最终 rename 共用事务；关联仍由原 publication finalizer 提交。"""
        from app.services.infrastructure.attachment_blob_catalog.locator import (
            attachment_directory_relative_locator,
        )
        from app.services.infrastructure.attachment_blob_catalog.records import (
            AttachmentFileVersion,
        )

        created = datetime.fromisoformat(snapshot.target_created_at)
        locator = attachment_directory_relative_locator(snapshot.target_attachment_id, created)
        claim = connection.execute("SELECT * FROM attachment_directory_fork_claims WHERE claim_id=?", (snapshot.claim_id,)).fetchone()
        if claim is None or _claim_snapshot(claim) != snapshot or claim["state"] not in {"prepared", "owner_reserved"}:
            raise RuntimeError("目录 fork 发布缺少有效的原冻结 claim")
        previous = self.get_directory(snapshot.target_attachment_id)
        if previous is not None:
            if (previous.relative_locator, previous.created_at, previous.availability) != (locator, created.isoformat(), "available"):
                raise RuntimeError("目录 fork 目标与原冻结 intent 不一致，不能覆盖")
        else:
            connection.execute("INSERT INTO attachment_directories VALUES (?,?,'available','private',?,?,NULL,NULL,NULL)", (snapshot.target_attachment_id, locator, created.isoformat(), datetime.now(UTC).isoformat()))
        for file in snapshot.files:
            if file.relative_path.split("/")[0] not in {"original", "preview"}:
                continue
            version = AttachmentFileVersion(snapshot.target_attachment_id, file.relative_path, file.relative_path.split("/")[0], file.digest, file.length,
                                            snapshot.file_name if file.relative_path == "original/content" else None,
                                            snapshot.mime_type if file.relative_path == "original/content" else "image/webp")
            current = self.file_version(snapshot.target_attachment_id, file.relative_path)
            if current is not None and current != version:
                raise RuntimeError("fork 导入的原件或预览与已发布版本冲突")
            if current is None:
                self._insert_file_version(connection, version)

    def terminal_fork_attachment_operations(self) -> tuple[str, ...]:
        return tuple(row[0] for row in self._connected().execute(
            "SELECT copy_operation_id FROM attachment_directory_fork_claims GROUP BY copy_operation_id "
            "HAVING SUM(state IN ('prepared','owner_reserved'))=0 ORDER BY copy_operation_id",
        ))

    def terminal_fork_attachment_snapshots(self, copy_operation_id: str) -> tuple[ForkAttachmentDirectorySnapshot, ...]:
        return tuple(_claim_snapshot(row) for row in self._connected().execute(
            "SELECT * FROM attachment_directory_fork_claims WHERE copy_operation_id=? AND state IN ('committed','aborted','target_deleted') ORDER BY claim_id",
            (copy_operation_id,),
        ))
