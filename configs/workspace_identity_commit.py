"""显式身份维护：复用原备份、CAS 发布和启动屏障，不开放运行时别名兼容。"""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack, closing
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast
from uuid import uuid4

import portalocker

from app.core.sqlite_state import SQLiteProcessOwnership
from app.gateway.control.gateway_registry.snapshot import read_workspace_registry
from app.gateway.control.gateway_state import GatewayStateStore
from configs.installer import atomic_write
from configs.runtime_logging_commit import (
    MigrationAsset,
    MigrationJournal,
    _backup_database,
    _connection,
    _digest,
    _json_bytes,
    _lock_databases,
    _publish,
)
from configs.workspace_identity_migration import (
    WorkspaceIdentityPreview,
    _registry_file,
    preview_workspace_identity_migration,
)
from configs.workspace_identity_storage import (
    database_digest,
    migrate_database,
    migrated_navigation,
    migrated_registry,
    replace_database,
    validate_registry_file,
)


class IdentityMigrationJournal(MigrationJournal):
    migration_id: str
    gateway_root: str
    recovery: dict[str, object]


def _checked_preview(root: Path, project_root: Path, verified: WorkspaceIdentityPreview | None) -> WorkspaceIdentityPreview:
    current = preview_workspace_identity_migration(gateway_root=root, project_root=project_root)
    if verified is not None:
        if (current.registry_digest != verified.registry_digest or current.references != verified.references
                or current.generator_imports != verified.generator_imports
                or tuple(replace(item, verified=False) for item in current.mappings)
                != tuple(replace(item, verified=False) for item in verified.mappings)):
            raise ValueError("身份预检后原登记、引用或身份已变化，请重新核验")
        if any(item.code != "remote_identity_unverified" for item in current.blockers):
            raise ValueError("锁内预检存在未结算状态或引用冲突，禁止发布")
        current = verified
    if current.registry_source is None or current.gateway_id is None:
        raise ValueError("没有可迁移的旧工作区登记")
    if current.blockers or not all(item.verified for item in current.mappings):
        raise ValueError("身份迁移存在冲突或未核验来源；先运行 preview-workspace-identity --verify-remote")
    return current


def _prepare(root: Path, preview: WorkspaceIdentityPreview, connections: dict[Path, sqlite3.Connection]) -> IdentityMigrationJournal:
    migration_id = str(uuid4())
    backup_root = root / "migration-backups/workspace-identity" / migration_id
    backup_root.mkdir(parents=True, mode=0o700)
    assets: list[MigrationAsset] = []
    database = root / "gateway.sqlite"
    after = backup_root / "gateway.after.sqlite"
    if database in connections:
        before = backup_root / "gateway.before.sqlite"
        _backup_database(database, before)
        atomic_write(after, before.read_bytes(), 0o600)
        with closing(_connection(after)) as target:
            target.execute("BEGIN IMMEDIATE")
            migrate_database(target, preview)
            target.commit()
            payload = read_workspace_registry(target)
            after_digest = database_digest(target)
        assets.append({"kind": "database", "path": str(database), "before": str(before), "after": str(after),
                       "before_digest": database_digest(connections[database]), "after_digest": after_digest})
    else:
        legacy = _registry_file(root / "workspaces.json")
        assert legacy is not None
        with GatewayStateStore(path=after) as store:
            store.replace_workspace_registry(migrated_registry(legacy, preview), expected_revision=0, owner="registry")
            payload = store.load_workspace_registry()
        assets.append({"kind": "file", "path": str(database), "before": str(backup_root / "gateway.absent"),
                       "after": str(after), "before_digest": None, "after_digest": _digest(after)})
    assert payload is not None and preview.gateway_id is not None
    candidate = backup_root / "registry.checked.json"
    atomic_write(candidate, _json_bytes(payload), 0o600)
    validate_registry_file(candidate, preview.gateway_id)
    files = {root / "workspaces.json": payload}
    navigation = root / "navigation/workspace-tree.json"
    source = _registry_file(navigation)
    if source is not None:
        files[navigation] = migrated_navigation(source, preview)
    for index, (path, content) in enumerate(files.items()):
        if not path.exists():
            continue
        before, after_file = backup_root / f"file-{index}.before", backup_root / f"file-{index}.after"
        original = path.read_bytes()
        atomic_write(before, original, 0o600)
        atomic_write(after_file, _json_bytes(content), 0o600)
        assets.append({"kind": "file", "path": str(path), "before": str(before), "after": str(after_file),
                       "before_digest": _digest(before), "after_digest": _digest(after_file)})
    return {
        "action": "migrate-workspace-identity", "state": "prepared", "migration_id": migration_id,
        "gateway_root": str(root), "workspaces": [], "settings": None,
        "assets": assets, "backup_root": str(backup_root),
        "guards": [str(root / "gateway.sqlite")], "databases": [str(database)],
        "format_notice": "迁移的控制面 JSON 已格式化；原字节和完整 SQLite 备份保留在 backup_root。",
        "recovery": {"format_version": 1, "migration_id": migration_id, "registry_gateway_id": preview.gateway_id,
                     "mappings": [{key: value for key, value in asdict(item).items()
                                   if key in {"legacy_workspace_id", "gateway_id", "workspace_id"}}
                                  for item in preview.mappings]},
    }


def commit_workspace_identity_migration(
    *, gateway_root: Path, project_root: Path, restore: bool = False,
    verified_preview: WorkspaceIdentityPreview | None = None, recovery_output: Path | None = None,
) -> dict[str, object]:
    root = gateway_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    journal_path = root / ".workspace-identity-migration.json"
    with ExitStack() as stack:
        handle = stack.enter_context((root / ".workspace-identity-migration.lock").open("a+b"))
        portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        stack.callback(portalocker.unlock, handle)
        journal = cast(IdentityMigrationJournal | None, _registry_file(journal_path))
        if journal is not None and journal["gateway_root"] != str(root):
            raise ValueError("身份迁移原件属于另一 Gateway 根，禁止重绑定")
        if restore and journal is None:
            raise ValueError("没有可恢复的身份迁移原件")
        database = root / "gateway.sqlite"
        if journal is not None and any(asset["path"] == str(database) and asset["kind"] == "file" for asset in journal["assets"]):
            ownership = SQLiteProcessOwnership(database)
            ownership.acquire()
            stack.callback(ownership.release)
            connections = {}
        else:
            connections = _lock_databases([database], stack)
        if journal is None or journal["state"] == "restored" and not restore:
            preview = _checked_preview(root, project_root, verified_preview)
            journal = _prepare(root, preview, connections)
            atomic_write(journal_path, _json_bytes(journal), 0o600)
        assert journal is not None
        _publish(journal_path, journal, connections, restore=restore,
                 database_digest=database_digest, replace_database=replace_database)
        recovery_file = recovery_output or Path(journal["backup_root"]) / "client-recovery.json"
        if not restore:
            content = _json_bytes(journal["recovery"])
            if recovery_file.exists() and recovery_file.read_bytes() != content:
                raise ValueError(f"客户端恢复输出已存在不同内容，迁移已完成；请指定另一输出路径: {recovery_file}")
            atomic_write(recovery_file, content, 0o600)
        return {"state": journal["state"], "migration_id": journal["migration_id"],
                "backup_root": journal["backup_root"], "format_notice": journal["format_notice"],
                "recovery_output": str(recovery_file) if not restore else None}
