"""在维护锁内发布唯一附件目录根；中断只能沿原迁移记录继续或撤销。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from app.core.atomic_fs import atomic_write_bytes, fsync_directory
from app.core.workspace_identity import read_workspace_id
from app.services.infrastructure.attachment_blob_catalog.directories import (
    protect_original,
)
from app.services.infrastructure.attachment_blob_catalog.directory_schema import (
    require_directory_schema,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.attachment_blob_catalog.migration.backup import (
    _copy,
    _json,
)
from app.services.infrastructure.attachment_blob_catalog.migration.catalog import (
    verify_converted_catalog,
)
from app.services.infrastructure.attachment_blob_catalog.migration.directories import (
    DirectoryPlan,
    _materialize,
    _plan,
    _verify_contents,
)
from app.services.infrastructure.attachment_blob_catalog.migration.preflight import (
    BackupSource,
    checked_path,
    file_digest,
    plan_backup,
    read_database,
    wal_digest,
)


def _database_content(path: Path, *, frozen: bool = False) -> str:
    """backup API 可改变页头；逐句 hash 核对 schema 与完整行值，不比较 SQLite 页布局。"""
    digest = hashlib.sha256()
    with read_database(path, frozen=frozen) as connection:
        for statement in connection.iterdump():
            data = statement.encode()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    return digest.hexdigest()


def _binding(backup: Path, plan: DirectoryPlan) -> dict[str, object]:
    return {"format_version": 1, "backup_id": plan.backup_id, "workspace_id": plan.workspace_id,
            "source_manifest_sha256": plan.source_manifest_sha256,
            "catalog_sha256": file_digest(checked_path(backup, "converted/catalog.sqlite"))}


def _locations(root: Path, backup_id: str) -> tuple[Path, Path, Path]:
    if len(backup_id) != 32 or any(char not in "0123456789abcdef" for char in backup_id):
        raise ValueError("附件离线安装 backup_id 无效")
    return (checked_path(root, "attachments"), checked_path(root, f".attachment-install-{backup_id}"),
            checked_path(root, f".attachments-{backup_id}.schema3"))


def _sources_match_backup(workspace: Path, backup: Path, plan: DirectoryPlan) -> list[dict[str, object]]:
    workspace_id, generation, sources = plan_backup(workspace)
    manifest = json.loads(checked_path(backup, "manifest.json").read_bytes())
    if (workspace_id, generation) != (plan.workspace_id, manifest["catalog_generation"]):
        raise RuntimeError("附件离线安装来源 workspace/catalog generation 已改变，须重新备份")
    rows = {row["locator"]: row for row in manifest["files"]}
    if set(rows) != {source.locator for source in sources}:
        raise RuntimeError("附件离线安装来源文件集合已改变，须重新备份")
    for source in sources:
        row = rows[source.locator]
        if source.kind != row["kind"]:
            raise RuntimeError("附件离线安装来源文件种类已改变")
        frozen = checked_path(backup / "data", source.locator)
        if source.kind == "sqlite":
            equal = _database_content(source.path) == _database_content(frozen, frozen=True)
        else:
            equal = source.digest == row["sha256"] and source.path.stat().st_size == row["length"]
        if not equal:
            raise RuntimeError(f"附件离线安装来源与备份内容不同: locator={source.locator}")
    return [{key: value for key, value in asdict(source).items() if key != "path"} for source in sources]


def _check_sources(root: Path, journal: dict[str, object], retired: Path) -> None:
    for source in journal["sources"]:
        locator = source["locator"]
        path = checked_path(retired, locator.removeprefix("attachments/")) if retired.exists() and locator.startswith("attachments/") else checked_path(root, locator)
        if file_digest(path) != source["digest"] or (source["kind"] == "sqlite" and wal_digest(path) != source["wal_digest"]):
            raise RuntimeError(f"附件离线安装期间原数据发生变化: locator={locator}")


def _verify_target(parent: Path, plan: DirectoryPlan, journal: dict[str, object]) -> None:
    _verify_contents(parent, plan, catalog=True)
    if file_digest(checked_path(parent, "attachments/catalog.sqlite")) != journal["catalog_sha256"]:
        raise RuntimeError("附件离线安装目标 catalog 已改变，不能覆盖或猜测成功")
    for resource in plan.resources:
        if resource.relative_locator is not None:
            original = checked_path(parent / "attachments", resource.relative_locator + "/original")
            if os.name != "nt" and (original.stat().st_mode & 0o222 or any(file.stat().st_mode & 0o222 for file in original.iterdir())):
                raise RuntimeError("附件离线安装目标原件尚未保护")


def _save(path: Path, journal: dict[str, object], state: str) -> None:
    journal["state"] = state
    atomic_write_bytes(path, _json(journal))


def install_converted_catalog(workspace: Path, backup: Path) -> dict[str, object]:
    """安装期间不启动业务 owner；发布前后的重试均不覆盖已有数据。"""
    workspace, backup = workspace.expanduser().resolve(strict=True), backup.expanduser().resolve(strict=True)
    root = workspace / ".boxteam"
    with AttachmentMaintenanceLock(root / "attachments", exclusive=True), AttachmentMaintenanceLock(backup / "converted", exclusive=False):
        verify_converted_catalog(backup)
        plan = _plan(backup)
        binding = _binding(backup, plan)
        if read_workspace_id(workspace) != plan.workspace_id:
            raise RuntimeError("附件离线安装备份不属于目标工作区")
        live, stage, retired = _locations(root, plan.backup_id)
        journal_path = checked_path(root, "attachment-migration.json")
        if any(backup.is_relative_to(path) or path.is_relative_to(backup) for path in (live, stage, retired)):
            raise ValueError("附件安装备份与在线/准备/保全目录重叠")
        if journal_path.exists():
            journal = json.loads(journal_path.read_bytes())
            if {key: journal.get(key) for key in binding} != binding:
                raise RuntimeError("附件离线安装记录与当前备份身份不同，拒绝改投")
            if journal["state"] == "installed":
                # 已发布后允许业务写入；重复 install 不能重放旧备份或覆盖新内容。
                with read_database(checked_path(live, "catalog.sqlite")) as catalog:
                    require_directory_schema(catalog)
                    owner = catalog.execute("SELECT workspace_id, schema_version FROM attachment_directory_metadata WHERE singleton_id=1").fetchone()
                    if owner is None or tuple(owner) != (plan.workspace_id, 4):
                        raise RuntimeError("已发布附件 catalog 的归属/版本不一致")
                return {**binding, "state": "installed"}
            if journal["state"] not in {"preparing", "ready"}:
                raise RuntimeError("附件离线安装已撤销或正在撤销；须沿原 rollback 收口")
        else:
            if stage.exists() or retired.exists():
                raise RuntimeError("附件离线安装目标缺少持久归属记录，拒绝吸收")
            sources = _sources_match_backup(workspace, backup, plan)
            journal = {**binding, "sources": sources}
            _save(journal_path, journal, "preparing")
        if journal["state"] == "preparing":
            _check_sources(root, journal, retired)
            if _sources_match_backup(workspace, backup, plan) != journal["sources"]:
                raise RuntimeError("附件离线安装准备期间来源身份改变")
            destination = stage / "attachments"
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            for resource in plan.resources:
                if resource.relative_locator is None:
                    continue
                for name in ("original", "preview", "derived"):
                    checked_path(destination, resource.relative_locator + "/" + name).mkdir(parents=True, exist_ok=True, mode=0o700)
                for file in resource.files:
                    _materialize(backup, destination, file)
                protect_original(checked_path(destination, resource.relative_locator))
            target = checked_path(destination, "catalog.sqlite")
            if not target.exists():
                source = checked_path(backup, "converted/catalog.sqlite")
                partial = checked_path(destination, "catalog.sqlite.partial")
                partial.unlink(missing_ok=True)
                _copy(BackupSource(source, "catalog.sqlite", "file", binding["catalog_sha256"]), partial)
                os.rename(partial, target)
                fsync_directory(destination)
            _verify_target(stage, plan, journal)
            for path in sorted((path for path in stage.rglob("*") if path.is_dir()), key=lambda item: len(item.parts), reverse=True):
                fsync_directory(path)
            fsync_directory(stage)
            fsync_directory(root)
            _check_sources(root, journal, retired)
            _save(journal_path, journal, "ready")
        _check_sources(root, journal, retired)
        if not retired.exists():
            if _sources_match_backup(workspace, backup, plan) != journal["sources"]:
                raise RuntimeError("附件离线安装切换前来源身份改变")
            _verify_target(stage, plan, journal)
            os.rename(live, retired)
            fsync_directory(root)
        if not live.exists():
            _verify_target(stage, plan, journal)
            os.rename(stage / "attachments", live)
            fsync_directory(stage)
            fsync_directory(root)
        _verify_target(root, plan, journal)
        _check_sources(root, journal, retired)
        _save(journal_path, journal, "installed")
        return {**binding, "state": "installed"}


def rollback_install(workspace: Path, backup: Path) -> dict[str, object]:
    """只撤销可证明未被后续写入的安装，原库和新目录均保全，不删除任一副本。"""
    workspace, backup = workspace.expanduser().resolve(strict=True), backup.expanduser().resolve(strict=True)
    root = workspace / ".boxteam"
    with AttachmentMaintenanceLock(root / "attachments", exclusive=True), AttachmentMaintenanceLock(backup / "converted", exclusive=False):
        verify_converted_catalog(backup)
        plan = _plan(backup)
        binding = _binding(backup, plan)
        live, stage, retired = _locations(root, plan.backup_id)
        journal_path = checked_path(root, "attachment-migration.json")
        journal = json.loads(journal_path.read_bytes())
        if read_workspace_id(workspace) != plan.workspace_id or {key: journal.get(key) for key in binding} != binding:
            raise RuntimeError("附件撤销记录与当前工作区/备份身份不同")
        if journal["state"] == "aborted":
            return {**binding, "state": "aborted"}
        if journal["state"] not in {"preparing", "ready", "installed", "rolling_back"}:
            raise RuntimeError("附件离线安装状态未知，拒绝撤销")
        _check_sources(root, journal, retired)
        if retired.exists() and live.exists():
            _verify_target(root, plan, journal)
        _save(journal_path, journal, "rolling_back")
        if retired.exists():
            if live.exists():
                discarded = checked_path(stage, "discarded")
                if discarded.exists():
                    raise RuntimeError("附件撤销已有保全的目标副本，拒绝覆盖")
                os.rename(live, discarded)
                fsync_directory(stage)
                fsync_directory(root)
            os.rename(retired, live)
            fsync_directory(root)
        _check_sources(root, journal, retired)
        _save(journal_path, journal, "aborted")
        return {**binding, "state": "aborted"}
