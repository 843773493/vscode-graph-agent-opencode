"""从已核验备份准备独立附件目录；不修改源数据或发布线上 schema。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.atomic_fs import atomic_write_bytes, fsync_directory
from app.services.infrastructure.attachment_blob_catalog.locator import (
    attachment_directory_relative_locator,
    resolve_attachment_directory,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.attachment_blob_catalog.migration.backup import (
    _copy,
    _json,
    verify_backup,
)
from app.services.infrastructure.attachment_blob_catalog.migration.preflight import (
    BackupSource,
    checked_path,
    file_digest,
    read_database,
)


@dataclass(frozen=True)
class DirectoryFile:
    source_locator: str
    relative_locator: str
    sha256: str
    length: int


@dataclass(frozen=True)
class DirectoryResource:
    attachment_id: str
    relative_locator: str | None
    source_owner: dict[str, object]
    source_blob: dict[str, object]
    files: tuple[DirectoryFile, ...]
    unattached_since: str | None
    gc_after: str | None


@dataclass(frozen=True)
class DirectoryPlan:
    backup_id: str
    workspace_id: str
    source_manifest_sha256: str
    resources: tuple[DirectoryResource, ...]
    format_version: int = 1
    source_attachment_schema: int = 3


def _timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("附件迁移时间必须包含时区，不能推测创建或释放时刻")
    return stamp.astimezone(UTC)


def _require_owner(data: Path, session_id: str, thread_id: str) -> None:
    with read_database(checked_path(data, "navigation/session-catalog.sqlite"), frozen=True) as catalog:
        row = catalog.execute(
            "SELECT storage_relative_locator FROM nodes WHERE node_id=? AND kind='session' AND state='active'",
            (session_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"活动附件缺少权威 Session owner: session_id={session_id}")
    with read_database(checked_path(data, row[0] + "/session-control.sqlite"), frozen=True) as control:
        if control.execute("SELECT 1 FROM thread_catalog WHERE thread_id=?", (thread_id,)).fetchone() is None:
            raise RuntimeError(f"活动附件缺少精确 Thread owner: session_id={session_id}, thread_id={thread_id}")


def _plan(directory: Path) -> DirectoryPlan:
    identity = verify_backup(directory)
    manifest = json.loads(checked_path(directory, "manifest.json").read_bytes())
    inputs = {row["locator"]: row for row in manifest["files"]}
    data = checked_path(directory, "data")
    resources = []
    with read_database(checked_path(data, "attachments/catalog.sqlite"), frozen=True) as catalog:
        for row in catalog.execute("SELECT * FROM attachment_owner_refs ORDER BY attachment_id"):
            owner = dict(row)
            attachment_id = owner["attachment_id"]
            blob_row = catalog.execute("SELECT * FROM attachment_blobs WHERE blob_id=?", (owner["blob_id"],)).fetchone()
            if blob_row is None or blob_row["digest"] != owner["digest"]:
                raise RuntimeError(f"附件迁移缺少一致正文版本: attachment_id={attachment_id}")
            blob = dict(blob_row)
            created = _timestamp(owner["created_at"])
            # 不从备份时间、原 blob 日期或用户文件名派生目录。
            locator = attachment_directory_relative_locator(attachment_id, created)
            resolve_attachment_directory(directory / "converted/attachments", locator)
            if owner["state"] == "active":
                _require_owner(data, owner["owner_session_id"], owner["owner_thread_id"])
                unattached_since = gc_after = None
            else:
                released = _timestamp(owner["updated_at"])
                unattached_since = released.isoformat()
                deadline = released + timedelta(days=30)
                if owner["retention_until"] is not None:
                    deadline = max(deadline, _timestamp(owner["retention_until"]))
                gc_after = deadline.isoformat()
            files = []
            if blob["availability"] == "available":
                for source_locator, child in (
                    ("attachments/" + blob["relative_locator"], "original/content"),
                    (f"attachments/.derived/{attachment_id}-512.webp", "preview/512.webp"),
                ):
                    source = inputs.get(source_locator)
                    if source is None:
                        if child == "original/content":
                            raise RuntimeError(f"附件迁移备份缺少原件: attachment_id={attachment_id}")
                        continue
                    if child == "original/content" and (
                        "sha256:" + source["sha256"] != blob["digest"] or source["length"] != blob["length"]
                    ):
                        raise RuntimeError(f"附件迁移原件版本不一致: attachment_id={attachment_id}")
                    files.append(DirectoryFile(source_locator, locator + "/" + child, source["sha256"], source["length"]))
            resources.append(DirectoryResource(
                attachment_id, locator if files else None, owner, blob, tuple(files), unattached_since, gc_after,
            ))
    return DirectoryPlan(identity["backup_id"], identity["workspace_id"], file_digest(directory / "manifest.json"), tuple(resources))


def _check_file(path: Path, row: DirectoryFile) -> None:
    if not path.is_file() or path.stat().st_size != row.length or file_digest(path) != row.sha256:
        raise RuntimeError(f"附件转换文件 hash/length 不一致，保留现场: locator={row.relative_locator}")


def _materialize(directory: Path, root: Path, row: DirectoryFile) -> None:
    target = checked_path(root, row.relative_locator)
    if target.exists():
        _check_file(target, row)
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = checked_path(root, row.relative_locator + ".partial")
    # 仅回收本转换 journal 名下未发布的临时副本；绝不覆盖已发布文件。
    partial.unlink(missing_ok=True)
    source = BackupSource(checked_path(directory / "data", row.source_locator), row.source_locator, "file", row.sha256)
    _copy(source, partial)
    _check_file(partial, row)
    os.replace(partial, target)
    fsync_directory(target.parent)


def _verify_contents(root: Path, plan: DirectoryPlan, *, catalog: bool = False) -> int:
    expected: set[str] = {"catalog.sqlite"} if catalog else set()
    expected_directories: set[str] = set()
    count = 0
    for resource in plan.resources:
        locator = resource.relative_locator
        if locator is None:
            continue
        for name in ("original", "preview", "derived"):
            child = locator + "/" + name
            path = checked_path(root / "attachments", child)
            if not path.is_dir():
                raise RuntimeError("附件转换缺少原件/预览/派生区")
            parts = child.split("/")
            expected_directories.update("/".join(parts[:end]) for end in range(1, len(parts) + 1))
        for row in resource.files:
            _check_file(checked_path(root / "attachments", row.relative_locator), row)
            expected.add(row.relative_locator)
            count += 1
    actual = set()
    actual_directories = set()
    for path in (root / "attachments").rglob("*"):
        locator = path.relative_to(root / "attachments").as_posix()
        checked_path(root / "attachments", locator)
        if path.is_dir():
            actual_directories.add(locator)
        else:
            actual.add(locator)
    if actual != expected or actual_directories != expected_directories:
        raise RuntimeError("附件转换包含未登记文件或目录，拒绝发布")
    return count


def _verify(root: Path, plan: DirectoryPlan) -> dict[str, object]:
    journal = json.loads(checked_path(root, "journal.json").read_bytes())
    if journal != {"format_version": 1, "state": "directories_prepared", "plan_sha256": file_digest(checked_path(root, "manifest.json"))}:
        raise RuntimeError("附件目录转换尚未完成或 journal 不一致")
    if checked_path(root, "manifest.json").read_bytes() != _json(asdict(plan)):
        raise RuntimeError("附件目录转换清单与来源备份不一致")
    count = _verify_contents(root, plan)
    return {"state": "directories_prepared", "backup_id": plan.backup_id, "workspace_id": plan.workspace_id,
            "resources": len(plan.resources), "directories": sum(resource.relative_locator is not None for resource in plan.resources),
            "files": count, "source_attachment_schema": 3}


def verify_directories(directory: Path) -> dict[str, object]:
    """校验转换产物与原备份的确定对应关系，不能把 preparing 当成可用。"""
    directory = directory.expanduser().resolve(strict=True)
    with AttachmentMaintenanceLock(directory / "converted", exclusive=False):
        return _verify(checked_path(directory, "converted"), _plan(directory))


def convert_backup(directory: Path) -> dict[str, object]:
    """在备份内准备目录，可从同一备份续跑；旧库/历史/线上 locator 保持原文。"""
    directory = directory.expanduser().resolve(strict=True)
    with AttachmentMaintenanceLock(directory / "converted", exclusive=True):
        plan = _plan(directory)
        root = checked_path(directory, "converted")
        payload = _json(asdict(plan))
        if root.exists():
            if checked_path(root, "manifest.json").read_bytes() != payload:
                raise RuntimeError("已有附件转换不属于当前备份，拒绝覆盖")
            journal = json.loads(checked_path(root, "journal.json").read_bytes())
            if journal["state"] == "directories_prepared":
                return _verify(root, plan)
            if journal != {"format_version": 1, "state": "preparing"}:
                raise RuntimeError("附件转换 journal 状态未知")
        else:
            # 初始化只包含可重建元数据；完整计划 hash 限定本次可回收的临时目录。
            preparing = checked_path(directory, ".converted-" + hashlib.sha256(payload).hexdigest() + ".preparing")
            if preparing.exists():
                if not preparing.is_dir():
                    raise RuntimeError("附件转换初始化位置不是普通目录，拒绝覆盖")
                shutil.rmtree(preparing)
            preparing.mkdir(mode=0o700)
            atomic_write_bytes(preparing / "manifest.json", payload)
            atomic_write_bytes(preparing / "journal.json", _json({"format_version": 1, "state": "preparing"}))
            fsync_directory(preparing)
            os.replace(preparing, root)
            fsync_directory(directory)
        attachments = checked_path(root, "attachments")
        attachments.mkdir(exist_ok=True, mode=0o700)
        for resource in plan.resources:
            if resource.relative_locator is None:
                continue
            for name in ("original", "preview", "derived"):
                checked_path(attachments, resource.relative_locator + "/" + name).mkdir(parents=True, exist_ok=True, mode=0o700)
            for row in resource.files:
                _materialize(directory, attachments, row)
        # 复制结束再次核验完整备份，不能混合转换期间变化的来源。
        if _json(asdict(_plan(directory))) != payload:
            raise RuntimeError("附件转换期间来源备份发生变化")
        _verify_contents(root, plan)
        for path in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
            fsync_directory(path)
        atomic_write_bytes(root / "journal.json", _json({"format_version": 1, "state": "directories_prepared", "plan_sha256": file_digest(root / "manifest.json")}))
        fsync_directory(directory)
        return _verify(root, plan)
