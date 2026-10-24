"""将已准备目录和旧历史绑定转换为隔离的 schema 4 catalog，不切换线上 owner。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from app.core.atomic_fs import atomic_write_bytes, fsync_directory, fsync_file
from app.core.sqlite_state import validate_sqlite_path_budget
from app.services.infrastructure.attachment_blob_catalog.directory_schema import (
    DIRECTORY_SCHEMA_SHA256,
    DIRECTORY_SCHEMA_VERSION,
    initialize_directory_schema,
    require_directory_schema,
)
from app.services.infrastructure.attachment_blob_catalog.legacy_references import (
    verify_legacy_references,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    attachment_directory_relative_locator,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.attachment_blob_catalog.migration.backup import _json
from app.services.infrastructure.attachment_blob_catalog.migration.directories import (
    DirectoryPlan,
    _plan,
    _timestamp,
    _verify,
)
from app.services.infrastructure.attachment_blob_catalog.migration.preflight import (
    checked_path,
    file_digest,
    read_database,
)
from app.services.infrastructure.attachment_blob_catalog.records import (
    AttachmentBlobRecord,
    AttachmentOwnerRef,
    _claim_snapshot,
    _mapping_snapshot,
)

# 列顺序只负责参数绑定与逐行核验；结构、约束和版本唯一定义在 SQL 资产。
_COLUMNS = {
    "attachment_directory_metadata": ("singleton_id", "workspace_id", "schema_version"),
    "attachment_directories": ("attachment_id", "relative_locator", "availability", "protection", "created_at", "updated_at", "unattached_since", "gc_after", "retention_until"),
    "attachment_directory_files": ("attachment_id", "relative_path", "role", "digest", "length", "file_name", "mime_type"),
    "attachment_directory_associations": ("attachment_id", "session_id", "thread_id", "binding_token", "state", "created_at", "updated_at"),
    "attachment_legacy_bindings": ("attachment_id", "owner_ref_id", "source_session_id", "source_thread_id", "source_blob_id", "source_owner_json", "source_blob_json"),
    "attachment_legacy_fork_payloads": ("record_kind", "record_key", "copy_operation_id", "metadata_json", "snapshot_json"),
}


def _source_rows(directory: Path, plan: DirectoryPlan) -> dict[str, list[tuple[object, ...]]]:
    rows: dict[str, list[tuple[object, ...]]] = {table: [] for table in _COLUMNS}
    rows["attachment_directory_metadata"].append((1, plan.workspace_id, DIRECTORY_SCHEMA_VERSION))
    for resource in plan.resources:
        owner = AttachmentOwnerRef(**resource.source_owner)
        blob = AttachmentBlobRecord(**resource.source_blob)
        created, updated = _timestamp(owner.created_at), _timestamp(owner.updated_at)
        locator = attachment_directory_relative_locator(resource.attachment_id, created)
        retention = None if owner.retention_until is None else _timestamp(owner.retention_until).isoformat()
        rows["attachment_directories"].append((
            resource.attachment_id, locator, blob.availability, blob.protection,
            created.isoformat(), updated.isoformat(), resource.unattached_since, resource.gc_after, retention,
        ))
        # 已回收目录也保留原件的版本事实，但不会建立物理目录或伪造可读字节。
        rows["attachment_directory_files"].append((
            resource.attachment_id, "original/content", "original", blob.digest, blob.length,
            owner.file_name, owner.mime_type or blob.mime_type,
        ))
        for file in resource.files:
            if file.relative_locator.endswith("/preview/512.webp"):
                rows["attachment_directory_files"].append((
                    resource.attachment_id, "preview/512.webp", "preview", "sha256:" + file.sha256,
                    file.length, None, "image/webp",
                ))
        rows["attachment_directory_associations"].append((
            resource.attachment_id, owner.owner_session_id, owner.owner_thread_id,
            "migration:" + plan.backup_id + ":" + owner.owner_ref_id, owner.state,
            created.isoformat(), updated.isoformat(),
        ))
        rows["attachment_legacy_bindings"].append((
            resource.attachment_id, owner.owner_ref_id, owner.owner_session_id, owner.owner_thread_id,
            blob.blob_id, _json(resource.source_owner).decode(), _json(resource.source_blob).decode(),
        ))
    with read_database(checked_path(directory / "data", "attachments/catalog.sqlite"), frozen=True) as source:
        for table, kind in (("fork_attachment_claims", "claim"), ("fork_attachment_mappings", "mapping")):
            for row in source.execute(f"SELECT * FROM {table}"):
                if kind == "claim":
                    if row["state"] not in {"committed", "aborted", "target_deleted"}:
                        raise RuntimeError("目录转换不能接管未终结的 fork claim")
                    _claim_snapshot(row)
                    key = row["claim_id"]
                else:
                    _mapping_snapshot(row)
                    key = hashlib.sha256(_json([row["copy_operation_id"], row["source_file_id"]])).hexdigest()
                metadata = dict(row)
                # snapshot_json 必须逐字保留，不能 asdict 后重新编码或改写旧 locator。
                snapshot_json = metadata.pop("snapshot_json")
                rows["attachment_legacy_fork_payloads"].append((kind, key, row["copy_operation_id"], _json(metadata).decode(), snapshot_json))
    return {table: sorted(values) for table, values in rows.items()}


def _write_catalog(path: Path, rows: dict[str, list[tuple[object, ...]]]) -> None:
    validate_sqlite_path_budget(path)
    with path.open("xb"):
        path.chmod(0o600)
    with closing(sqlite3.connect(path, isolation_level=None)) as target:
        target.execute("PRAGMA foreign_keys=ON")
        target.execute("PRAGMA synchronous=FULL")
        initialize_directory_schema(target)
        target.execute("BEGIN IMMEDIATE")
        for table, columns in _COLUMNS.items():
            target.executemany(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", rows[table],
            )
        target.execute("COMMIT")
    fsync_file(path)


def _verify_catalog(path: Path, rows: dict[str, list[tuple[object, ...]]]) -> None:
    with read_database(path, frozen=True) as target:
        require_directory_schema(target)
        for table, columns in _COLUMNS.items():
            actual = sorted(tuple(row) for row in target.execute(f"SELECT {','.join(columns)} FROM {table}"))
            if actual != rows[table]:
                raise RuntimeError(f"附件目录 catalog 与来源记录不一致: table={table}")
        verify_legacy_references(target)


def _binding(root: Path, plan: DirectoryPlan) -> dict[str, object]:
    return {
        "format_version": 1, "backup_id": plan.backup_id, "workspace_id": plan.workspace_id,
        "source_manifest_sha256": plan.source_manifest_sha256,
        "directory_manifest_sha256": file_digest(checked_path(root, "manifest.json")),
        "target_schema": DIRECTORY_SCHEMA_VERSION, "schema_sha256": DIRECTORY_SCHEMA_SHA256,
    }


def _result(plan: DirectoryPlan, rows: dict[str, list[tuple[object, ...]]]) -> dict[str, object]:
    return {"state": "catalog_prepared", "backup_id": plan.backup_id, "workspace_id": plan.workspace_id,
            "source_attachment_schema": 3, "target_attachment_schema": DIRECTORY_SCHEMA_VERSION,
            "resources": len(plan.resources), "legacy_fork_payloads": len(rows["attachment_legacy_fork_payloads"])}


def verify_converted_catalog(directory: Path) -> dict[str, object]:
    """只读核验已发布的目标 catalog 及其来源，不访问线上工作区。"""
    directory = directory.expanduser().resolve(strict=True)
    root = checked_path(directory, "converted")
    with AttachmentMaintenanceLock(root, exclusive=False):
        plan = _plan(directory)
        _verify(root, plan)
        rows = _source_rows(directory, plan)
        path = checked_path(root, "catalog.sqlite")
        journal = json.loads(checked_path(root, "catalog-journal.json").read_bytes())
        if journal.get("state") != "catalog_prepared":
            raise RuntimeError("附件目录 catalog 尚未完成")
        if journal != {**_binding(root, plan), "state": "catalog_prepared", "catalog_sha256": file_digest(path)}:
            raise RuntimeError("附件目录 catalog 未完成、来源冲突或文件 hash 不一致")
        _verify_catalog(path, rows)
        return _result(plan, rows)


def prepare_converted_catalog(directory: Path) -> dict[str, object]:
    """原件目录核验后准备 schema 4，失败只重建本 journal 名下的未发布临时库。"""
    directory = directory.expanduser().resolve(strict=True)
    root = checked_path(directory, "converted")
    with AttachmentMaintenanceLock(root, exclusive=True):
        plan = _plan(directory)
        _verify(root, plan)
        rows = _source_rows(directory, plan)
        binding = _binding(root, plan)
        journal_path = checked_path(root, "catalog-journal.json")
        path = checked_path(root, "catalog.sqlite")
        if journal_path.exists():
            journal = json.loads(journal_path.read_bytes())
            if journal.get("state") == "catalog_prepared":
                if journal != {**binding, "state": "catalog_prepared", "catalog_sha256": file_digest(path)}:
                    raise RuntimeError("已有目录 catalog 的来源/schema/hash 不一致，拒绝覆盖")
                _verify_catalog(path, rows)
                return _result(plan, rows)
            if journal != {**binding, "state": "preparing"}:
                raise RuntimeError("已有目录 catalog 的准备身份不一致，拒绝覆盖")
        else:
            if path.exists():
                raise RuntimeError("目录 catalog 缺少持久准备记录，拒绝猜测归属")
            atomic_write_bytes(journal_path, _json({**binding, "state": "preparing"}))
        if not path.exists():
            partial = checked_path(root, "catalog.sqlite.partial")
            for suffix in ("", "-journal"):
                checked_path(root, partial.name + suffix).unlink(missing_ok=True)
            _write_catalog(partial, rows)
            _verify_catalog(partial, rows)
            # 只有源备份和物化目录仍全部一致，临时 catalog 才可发布。
            current = _plan(directory)
            _verify(root, current)
            if _binding(root, current) != binding or _source_rows(directory, current) != rows:
                raise RuntimeError("准备目录 catalog 期间来源发生变化")
            os.replace(partial, path)
            fsync_directory(root)
        # 覆盖 rename 已完成而 journal 尚未发布时的中断，绝不覆盖成品库。
        _verify_catalog(path, rows)
        atomic_write_bytes(journal_path, _json({**binding, "state": "catalog_prepared", "catalog_sha256": file_digest(path)}))
        # TODO: 迁齐线上上传/读取/fork 与 ingest 表后，使用此结构完成唯一切换点。
        return _result(plan, rows)
