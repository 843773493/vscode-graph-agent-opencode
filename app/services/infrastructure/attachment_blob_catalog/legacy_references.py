"""从目录 catalog 的不可变来源解析旧引用，不访问旧库或旧正文位置。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.core.resource_uri import parse_session_artifact_uri, session_artifact_uri
from app.services.infrastructure.attachment_blob_catalog.locator import (
    attachment_directory_relative_locator,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    AttachmentBlobRecord,
    AttachmentOwnerRef,
    ForkAttachmentSnapshot,
    _claim_snapshot,
    _mapping_snapshot,
)


@dataclass(frozen=True, slots=True)
class LegacyAttachmentReference:
    """旧历史身份对应的目录原件版本；没有原生路径或新的历史身份。"""

    attachment_id: str
    relative_locator: str
    digest: str
    length: int
    file_name: str | None
    mime_type: str | None
    availability: str


def _binding(connection: sqlite3.Connection, attachment_id: str) -> tuple[LegacyAttachmentReference, AttachmentOwnerRef, AttachmentBlobRecord]:
    row = connection.execute(
        "SELECT * FROM attachment_legacy_bindings WHERE attachment_id=?", (attachment_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("旧附件引用缺少目录来源绑定")
    owner = AttachmentOwnerRef(**json.loads(row["source_owner_json"]))
    blob = AttachmentBlobRecord(**json.loads(row["source_blob_json"]))
    if (row["attachment_id"], row["owner_ref_id"], row["source_session_id"], row["source_thread_id"], row["source_blob_id"], owner.blob_id, owner.digest) != (
        owner.attachment_id, owner.owner_ref_id, owner.owner_session_id, owner.owner_thread_id, blob.blob_id, blob.blob_id, blob.digest,
    ):
        raise RuntimeError("旧附件来源绑定与冻结 owner/blob 不一致")
    resource = connection.execute(
        "SELECT * FROM attachment_directories WHERE attachment_id=?", (attachment_id,),
    ).fetchone()
    original = connection.execute(
        "SELECT * FROM attachment_directory_files WHERE attachment_id=? AND relative_path='original/content'", (attachment_id,),
    ).fetchone()
    if resource is None or original is None:
        raise RuntimeError("旧附件来源缺少目标目录或原件版本")
    if (resource["relative_locator"], original["role"], original["digest"], original["length"], original["file_name"], original["mime_type"]) != (
        attachment_directory_relative_locator(attachment_id, datetime.fromisoformat(owner.created_at)),
        "original", blob.digest, blob.length, owner.file_name, owner.mime_type or blob.mime_type,
    ):
        raise RuntimeError("旧附件目录或原件版本与冻结来源不一致")
    return LegacyAttachmentReference(
        attachment_id, resource["relative_locator"], blob.digest, blob.length,
        original["file_name"], original["mime_type"], resource["availability"],
    ), owner, blob


def _mapped_reference(connection: sqlite3.Connection, row: sqlite3.Row, workspace_id: str) -> LegacyAttachmentReference | None:
    metadata = json.loads(row["metadata_json"])
    snapshot = _mapping_snapshot({**metadata, "snapshot_json": row["snapshot_json"]})
    if metadata["copy_operation_id"] != row["copy_operation_id"]:
        raise RuntimeError("旧附件 alias 的 operation 身份不一致")
    if snapshot.claim_id is not None:
        claim = connection.execute(
            "SELECT * FROM attachment_legacy_fork_payloads WHERE record_kind='claim' AND record_key=?",
            (snapshot.claim_id,),
        ).fetchone()
        if claim is None:
            raise RuntimeError("旧附件 alias 缺少冻结 claim")
        claim_metadata = json.loads(claim["metadata_json"])
        claimed = _claim_snapshot({**claim_metadata, "snapshot_json": claim["snapshot_json"]})
        if (claim_metadata["state"], claim["copy_operation_id"], claim_metadata["copy_operation_id"], claimed.to_payload()) != (
            "committed", row["copy_operation_id"], row["copy_operation_id"], snapshot.to_payload(),
        ):
            raise RuntimeError("旧附件 alias 与已发布 claim 不一致")
    if snapshot.availability != "available":
        # 可选省略仍保留冻结原因，不能借相同 digest 关联到其它可读资源。
        return None
    reference, owner, blob = _binding(connection, snapshot.target_attachment_id)
    _require_target_version(snapshot, owner, blob, workspace_id)
    return reference


def _require_target_version(snapshot: ForkAttachmentSnapshot, owner: AttachmentOwnerRef, blob: AttachmentBlobRecord, workspace_id: str) -> None:
    target = parse_session_artifact_uri(snapshot.target_file_id)
    if (target.session_artifact_kind, target.scope_id, target.tail_segments[0], target.tail_segments[2]) != (
        "attachments", workspace_id, snapshot.target_session_id, snapshot.target_attachment_id,
    ):
        raise RuntimeError("旧附件 alias 的目标 URI 与冻结 owner 不一致")
    # 快照 locator 属于历史来源机器；目标通过迁入的 owner/version 定位，不能比较两端物理位置。
    if (owner.owner_ref_id, owner.owner_session_id, owner.owner_thread_id, owner.blob_id, owner.digest, owner.variant, owner.item_ref, blob.length) != (
        snapshot.target_owner_ref_id, snapshot.target_session_id, snapshot.target_owner_thread_id,
        snapshot.blob_id, snapshot.digest, snapshot.variant, snapshot.target_item_ref, snapshot.length,
    ):
        raise RuntimeError("旧附件 alias 的目标 owner 或正文版本不一致")


def resolve_legacy_attachment(
    connection: sqlite3.Connection, *, session_id: str, thread_id: str, file_id: str,
    workspace_id: str, gateway_id: str | None = None,
) -> LegacyAttachmentReference | None:
    """优先精确消费 Thread 的冻结 alias，否则只认本地精确旧 owner。"""
    stored_workspace = connection.execute("SELECT workspace_id FROM attachment_directory_metadata WHERE singleton_id=1").fetchone()
    if stored_workspace is None or stored_workspace[0] != workspace_id:
        raise ValueError("附件目录 catalog 不属于目标工作区")
    rows = connection.execute(
        "SELECT * FROM attachment_legacy_fork_payloads WHERE record_kind='mapping' "
        "AND json_extract(metadata_json,'$.target_session_id')=? "
        "AND json_extract(metadata_json,'$.target_thread_id')=? "
        "AND json_extract(metadata_json,'$.source_file_id')=? LIMIT 2", (session_id, thread_id, file_id),
    ).fetchall()
    if len(rows) > 1:
        raise RuntimeError("旧附件 target-local alias 歧义")
    if rows:
        return _mapped_reference(connection, rows[0], workspace_id)
    parsed = parse_session_artifact_uri(file_id, local_gateway_id=gateway_id)
    if parsed.session_artifact_kind != "attachments":
        raise ValueError("旧附件引用必须精确指向附件产物")
    if parsed.gateway_authority is not None or parsed.scope_id != workspace_id or parsed.tail_segments[0] != session_id:
        return None
    row = connection.execute(
        "SELECT attachment_id FROM attachment_legacy_bindings WHERE attachment_id=? AND source_session_id=? AND source_thread_id=?",
        (parsed.tail_segments[2], session_id, thread_id),
    ).fetchone()
    return None if row is None else _binding(connection, row[0])[0]


def verify_legacy_references(connection: sqlite3.Connection) -> None:
    """迁移发布前实际解析全部旧 owner、alias 和 claim，拒绝保存不可消费的历史。"""
    workspace_id = connection.execute("SELECT workspace_id FROM attachment_directory_metadata WHERE singleton_id=1").fetchone()[0]
    for row in connection.execute("SELECT attachment_id,source_session_id,source_thread_id FROM attachment_legacy_bindings"):
        reference = resolve_legacy_attachment(
            connection, session_id=row["source_session_id"], thread_id=row["source_thread_id"],
            file_id=session_artifact_uri(workspace_id, row["source_session_id"], "attachments", row["attachment_id"]),
            workspace_id=workspace_id,
        )
        if reference is None or reference.attachment_id != row["attachment_id"]:
            raise RuntimeError("旧附件 owner 无法唯一解析到迁入目录")
    for row in connection.execute("SELECT * FROM attachment_legacy_fork_payloads"):
        metadata = json.loads(row["metadata_json"])
        if row["record_kind"] == "claim":
            snapshot = _claim_snapshot({**metadata, "snapshot_json": row["snapshot_json"]})
            if metadata["copy_operation_id"] != row["copy_operation_id"] or metadata["claim_id"] != row["record_key"] or metadata["state"] not in {"committed", "aborted", "target_deleted"}:
                raise RuntimeError("旧附件 claim 的持久身份或终态不一致")
            if metadata["state"] == "committed":
                _, owner, blob = _binding(connection, snapshot.target_attachment_id)
                _require_target_version(snapshot, owner, blob, workspace_id)
        else:
            resolve_legacy_attachment(
                connection, session_id=metadata["target_session_id"], thread_id=metadata["target_thread_id"],
                file_id=metadata["source_file_id"], workspace_id=workspace_id,
            )
