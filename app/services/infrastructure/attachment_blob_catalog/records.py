"""目录资源行与显式冻结快照；旧 blob/owner 只供迁移和封存历史解码。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from app.core.resource_uri import parse_attachment_uri
from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    validate_digest,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    attachment_directory_relative_locator,
    validate_attachment_file_segments,
)


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


@dataclass(frozen=True, slots=True)
class ForkAttachmentSnapshot:
    """冻结附件 backing owner、目标消费 scope 和保活 owner；只携带 blob 坐标。"""

    source_attachment_id: str
    source_file_id: str
    source_owner_ref_id: str
    # 正文 backing owner 可能不同于用户选择的来源 Thread。
    source_session_id: str
    source_thread_id: str
    target_attachment_id: str
    target_file_id: str
    target_owner_ref_id: str
    # 目标消费 scope；复用 main 正文时不等于下方保活 Thread。
    target_session_id: str
    target_thread_id: str
    # 消费者 Thread 与正文保活 Thread 分离；复用 main 引用时两者不同。
    target_owner_thread_id: str
    source_item_ref: str | None
    target_item_ref: str | None
    blob_id: str
    # 摘要格式为 sha256:<hex>，正文长度的单位为 bytes。
    digest: str
    length: int
    relative_locator: str
    variant: str
    catalog_revision: int
    file_name: str | None
    mime_type: str | None
    availability: str
    required: bool
    claim_id: str | None
    source_file_aliases: tuple[str, ...] = ()
    target_action: str = "retain_reference"
    unavailable_reason: str | None = None


    def to_payload(self) -> dict[str, object]:
        """显式冻结 v2 载荷；新增运行态字段不能隐式改变已封存 hash。"""
        # 目录快照须登记新版本；此处不能改用 asdict 或随 dataclass 字段扩展。
        return {
            "source_attachment_id": self.source_attachment_id,
            "source_file_id": self.source_file_id,
            "source_owner_ref_id": self.source_owner_ref_id,
            "source_session_id": self.source_session_id,
            "source_thread_id": self.source_thread_id,
            "target_attachment_id": self.target_attachment_id,
            "target_file_id": self.target_file_id,
            "target_owner_ref_id": self.target_owner_ref_id,
            "target_session_id": self.target_session_id,
            "target_thread_id": self.target_thread_id,
            "target_owner_thread_id": self.target_owner_thread_id,
            "source_item_ref": self.source_item_ref,
            "target_item_ref": self.target_item_ref,
            "blob_id": self.blob_id,
            "digest": self.digest,
            "length": self.length,
            "relative_locator": self.relative_locator,
            "variant": self.variant,
            "catalog_revision": self.catalog_revision,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
            "availability": self.availability,
            "required": self.required,
            "claim_id": self.claim_id,
            "source_file_aliases": list(self.source_file_aliases),
            "target_action": self.target_action,
            "unavailable_reason": self.unavailable_reason,
        }


def _claim_snapshot(row: sqlite3.Row | Mapping[str, str]) -> ForkAttachmentSnapshot:
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


def _mapping_snapshot(row: sqlite3.Row | Mapping[str, str]) -> ForkAttachmentSnapshot:
    """在线 alias 与迁移历史共用精确消费 owner/别名校验，不重写冻结原文。"""
    snapshot = ForkAttachmentSnapshot(**json.loads(row["snapshot_json"]))
    if (row["target_session_id"], row["target_thread_id"]) != (snapshot.target_session_id, snapshot.target_thread_id) or row["source_file_id"] not in {
        snapshot.source_file_id, *snapshot.source_file_aliases, snapshot.target_file_id,
    }:
        raise RuntimeError("fork attachment mapping 与冻结 owner/alias 不一致")
    return snapshot


@dataclass(frozen=True, slots=True)
class AttachmentDirectoryRecord:
    attachment_id: str
    relative_locator: str
    availability: str
    protection: str
    created_at: str
    updated_at: str
    unattached_since: str | None
    gc_after: str | None
    retention_until: str | None


@dataclass(frozen=True, slots=True)
class AttachmentFileVersion:
    attachment_id: str
    relative_path: str
    role: str
    digest: str
    length: int
    file_name: str | None
    mime_type: str | None


@dataclass(frozen=True, slots=True)
class DirectoryIngestRecord:
    ingest_idempotency_key: str
    ingest_id: str
    attachment_id: str
    owner_session_id: str
    owner_thread_id: str
    message_id: str
    pin_lease_id: str
    pin_fencing_token: int
    pin_captured_generation: int
    preimage_hash: str
    staging_relative_locator: str
    relative_locator: str
    digest: str
    length: int
    file_name: str | None
    mime_type: str
    state: str
    abort_reason: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DirectorySnapshotFile:
    relative_path: str
    digest: str
    length: int

    def __post_init__(self) -> None:
        if not isinstance(self.relative_path, str):
            raise TypeError("目录快照文件路径必须是字符串")
        validate_attachment_file_segments(tuple(self.relative_path.split("/")))
        validate_digest(self.digest)
        if type(self.length) is not int or self.length < 0:
            raise ValueError("目录快照文件长度必须是非负整数")


@dataclass(frozen=True, slots=True)
class ForkAttachmentDirectorySnapshot:
    """新目录冻结合同；显式版本，不扩展或重算旧 v2 blob 载荷。"""

    source_attachment_id: str
    source_file_id: str
    source_session_id: str
    source_thread_id: str
    target_attachment_id: str
    target_file_id: str
    target_session_id: str
    target_thread_id: str
    target_created_at: str
    file_name: str | None
    mime_type: str | None
    files: tuple[DirectorySnapshotFile, ...]
    directories: tuple[str, ...]
    availability: str
    required: bool
    target_action: str
    claim_id: str | None
    source_file_aliases: tuple[str, ...] = ()
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        created = datetime.fromisoformat(self.target_created_at)
        attachment_directory_relative_locator(self.source_attachment_id, created)
        attachment_directory_relative_locator(self.target_attachment_id, created)
        if (type(self.required) is not bool or self.availability not in {"available", "unavailable"}
                or not isinstance(self.files, tuple) or not all(isinstance(file, DirectorySnapshotFile) for file in self.files)
                or not isinstance(self.directories, tuple) or not isinstance(self.source_file_aliases, tuple)):
            raise ValueError("目录快照必须使用 typed availability、文件和目录清单")
        paths = tuple(file.relative_path for file in self.files)
        if tuple(sorted(set(paths))) != paths or tuple(sorted(set(self.directories))) != self.directories:
            raise ValueError("目录快照清单必须排序且无重复")
        directories = set(self.directories)
        if directories.intersection(paths):
            raise ValueError("目录快照不能把同一路径同时声明为文件和目录")
        for path in (*paths, *self.directories):
            if not isinstance(path, str):
                raise TypeError("目录快照路径必须是字符串")
            parts = tuple(path.split("/"))
            validate_attachment_file_segments(parts)
            if parts[0] not in {"original", "preview", "derived"}:
                raise ValueError("目录快照只能包含原件、预览和派生区")
            if any("/".join(parts[:depth]) not in directories for depth in range(1, len(parts))):
                raise ValueError("目录快照缺少明确的父目录")
        aliases = (self.source_file_id, *self.source_file_aliases)
        if len(set(aliases)) != len(aliases):
            raise ValueError("目录快照的来源 alias 重复")
        for uri in aliases:
            parse_attachment_uri(uri, require_file=True)
        target = parse_attachment_uri(self.target_file_id, require_file=True)
        if self.availability == "available":
            if (self.claim_id is None or self.target_action not in {"retain_reference", "reuse_reference", "copy_snapshot"}
                    or target.kind != "attachments" or target.attachment_id != self.target_attachment_id
                    or "original/content" not in paths or not {"original", "preview", "derived"}.issubset(directories)
                    or self.unavailable_reason is not None):
                raise ValueError("可用目录快照缺少原件、claim 或精确目标身份")
            if "/".join(target.tail_segments[1:]) not in paths:
                raise ValueError("目录快照的目标文件未在冻结清单中")
        elif (self.files or self.directories or self.claim_id is not None
              or self.target_action not in {"omit", "unsupported"} or not self.unavailable_reason or self.required):
            raise ValueError("不可用目录快照不能保留正文、claim 或必需依赖")

    def to_payload(self) -> dict[str, object]:
        return {
            "format_version": "attachment-directory:v1",
            "source_attachment_id": self.source_attachment_id,
            "source_file_id": self.source_file_id,
            "source_session_id": self.source_session_id,
            "source_thread_id": self.source_thread_id,
            "target_attachment_id": self.target_attachment_id,
            "target_file_id": self.target_file_id,
            "target_session_id": self.target_session_id,
            "target_thread_id": self.target_thread_id,
            "target_created_at": self.target_created_at,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
            "files": [{"relative_path": file.relative_path, "digest": file.digest, "length": file.length} for file in self.files],
            "directories": list(self.directories),
            "availability": self.availability,
            "required": self.required,
            "target_action": self.target_action,
            "claim_id": self.claim_id,
            "source_file_aliases": list(self.source_file_aliases),
            "unavailable_reason": self.unavailable_reason,
        }


def attachment_snapshot_from_payload(payload: Mapping[str, object]) -> ForkAttachmentSnapshot | ForkAttachmentDirectorySnapshot:
    """只解码明确登记的历史版本；旧版本继续保留原 to_payload/hash。"""
    version = payload.get("format_version")
    if version is None:
        return ForkAttachmentSnapshot(**payload)
    if version != "attachment-directory:v1":
        raise ValueError("未知附件快照版本")
    values = dict(payload)
    values.pop("format_version")
    values["files"] = tuple(DirectorySnapshotFile(**item) for item in values["files"])
    values["source_file_aliases"] = tuple(values["source_file_aliases"])
    values["directories"] = tuple(values["directories"])
    return ForkAttachmentDirectorySnapshot(**values)
