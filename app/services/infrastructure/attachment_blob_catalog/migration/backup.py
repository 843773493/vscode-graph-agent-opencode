"""离线迁移的原件、SQLite 和冻结历史备份；成功不代表 schema 已切换。"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core.atomic_fs import atomic_write_bytes, fsync_directory, fsync_file
from app.core.sqlite_state import sqlite_file_uri
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.attachment_blob_catalog.migration.preflight import (
    SOURCE_ATTACHMENT_SCHEMA,
    BackupSource,
    checked_path,
    file_digest,
    plan_backup,
    read_database,
    wal_digest,
)


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _unchanged(source: BackupSource) -> None:
    current_wal = wal_digest(source.path) if source.kind == "sqlite" else None
    if file_digest(source.path) != source.digest or current_wal != source.wal_digest:
        raise RuntimeError(f"离线备份输入发生变化，须保留原数据并重新准备: locator={source.locator}")


def _copy(source: BackupSource, target: Path) -> dict[str, object]:
    _unchanged(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 明确创建私有目标且不覆盖；SQLite 后续只写这个已创建的文件。
    with target.open("xb") as stream:
        target.chmod(0o600)
        if source.kind == "file":
            with source.path.open("rb") as original:
                for chunk in iter(lambda: original.read(1024 * 1024), b""):
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    if source.kind == "sqlite":
        with read_database(source.path) as original, closing(sqlite3.connect(sqlite_file_uri(target), uri=True)) as snapshot:
            original.backup(snapshot)
            snapshot.commit()
        fsync_file(target)
        with read_database(target, frozen=True):
            pass
    digest = file_digest(target)
    _unchanged(source)
    if source.kind == "file" and digest != source.digest:
        raise RuntimeError(f"离线备份复制 hash 不一致: locator={source.locator}")
    fsync_directory(target.parent)
    return {"locator": source.locator, "kind": source.kind, "length": target.stat().st_size, "sha256": digest}


def _verify_files(directory: Path, manifest: dict[str, object]) -> None:
    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("离线备份清单缺少文件")
    seen: set[str] = set()
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("locator"), str):
            raise TypeError("离线备份文件记录非法")
        locator = row["locator"]
        if locator in seen:
            raise ValueError("离线备份清单存在重复 locator")
        seen.add(locator)
        path = checked_path(directory / "data", locator)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError(f"离线备份输入不是普通文件: locator={locator}")
        if path.stat().st_size != row["length"] or file_digest(path) != row["sha256"]:
            raise RuntimeError(f"离线备份文件 hash/length 不一致: locator={locator}")
        if row["kind"] == "sqlite":
            with read_database(path, frozen=True):
                pass
        elif row["kind"] != "file":
            raise ValueError("离线备份文件种类未知")


def verify_backup(directory: Path) -> dict[str, object]:
    """只读核验已发布的备份；preparing/缺失清单不得冒充成功。"""
    directory = directory.expanduser().resolve(strict=True)
    journal = json.loads(checked_path(directory, "journal.json").read_bytes())
    if journal["format_version"] != 1 or journal["state"] != "prepared":
        raise RuntimeError("离线附件备份尚未完成或格式未知")
    manifest_path = checked_path(directory, "manifest.json")
    if file_digest(manifest_path) != journal["manifest_sha256"]:
        raise RuntimeError("离线附件备份清单 hash 不一致")
    manifest = json.loads(manifest_path.read_bytes())
    if (
        manifest["format_version"] != 1 or manifest["source_attachment_schema"] != SOURCE_ATTACHMENT_SCHEMA
        or manifest["backup_id"] != journal["backup_id"] or manifest["workspace_id"] != journal["workspace_id"]
    ):
        raise RuntimeError("离线附件备份身份或 schema 不一致")
    _verify_files(directory, manifest)
    return {"state": "prepared", "backup_id": journal["backup_id"], "workspace_id": journal["workspace_id"],
            "source_attachment_schema": SOURCE_ATTACHMENT_SCHEMA, "files": len(manifest["files"])}


def prepare_backup(workspace: Path, directory: Path) -> dict[str, object]:
    """持独占锁完成只读预检和一致备份；失败保留已生成产物，绝不修改源库。"""
    workspace = workspace.expanduser().resolve(strict=True)
    directory = directory.expanduser().resolve()
    root = workspace / ".boxteam"
    if directory.exists():
        raise FileExistsError("离线备份目标已存在，拒绝覆盖；可使用 verify 核验已有备份")
    if workspace.is_relative_to(directory) or any(directory.is_relative_to(root / name) for name in ("attachments", "sessions", "navigation")):
        raise ValueError("离线备份目标与来源资源或历史目录重叠")
    with AttachmentMaintenanceLock(root / "attachments", exclusive=True):
        workspace_id, generation, sources = plan_backup(workspace)
        directory.mkdir(parents=True, mode=0o700)
        journal = {"format_version": 1, "backup_id": uuid4().hex, "workspace_id": workspace_id,
                   "state": "preparing", "created_at": datetime.now(UTC).isoformat()}
        atomic_write_bytes(directory / "journal.json", _json(journal))
        files = [_copy(source, checked_path(directory / "data", source.locator)) for source in sources]
        # 长复制窗口结束后再核对全部输入，不能把早期复制的旧文件与后期的新状态混合。
        for source in sources:
            _unchanged(source)
        manifest = {"format_version": 1, "backup_id": journal["backup_id"], "workspace_id": workspace_id,
                    "source_attachment_schema": SOURCE_ATTACHMENT_SCHEMA, "catalog_generation": generation,
                    "created_at": journal["created_at"], "files": files}
        atomic_write_bytes(directory / "manifest.json", _json(manifest))
        _verify_files(directory, manifest)
        # 所有目录项与内容 durable 后，唯一 journal 发布备份完成。
        for path in sorted((path for path in directory.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
            fsync_directory(path)
        journal.update(state="prepared", manifest_sha256=file_digest(directory / "manifest.json"))
        atomic_write_bytes(directory / "journal.json", _json(journal))
        fsync_directory(directory.parent)
        # TODO: 备份之后的 schema 3 → 4 转换、持久切换点与继续/撤销由此接续，
        # 当前只准备可核验的源快照，不提升 schema 或改变线上 locator。
        return verify_backup(directory)
