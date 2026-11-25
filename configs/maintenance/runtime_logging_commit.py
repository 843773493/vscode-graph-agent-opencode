"""显式离线迁移：原件备份、逐资产 CAS 发布及中断后的继续／恢复。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import ExitStack, closing
from pathlib import Path
from typing import NotRequired, TypedDict, cast
from uuid import uuid4

import jsonschema
import portalocker
import psutil

from app.core.config_migration_guard import config_migration_marker
from app.core.config_sources import (
    StableConfigFile,
    config_revision,
    read_stable_config_file,
)
from app.core.sqlite_state import SQLiteProcessOwnership, sqlite_file_uri
from app.core.workspace_identity import read_workspace_id
from app.services.infrastructure.config.state import (
    restore_environment_secret_references,
)
from configs.installer import atomic_write, resolve_config_resource_source
from configs.maintenance.runtime_logging_migration import (
    ConfigDomain,
    RuntimeLoggingMigrationPlan,
    preview_runtime_logging_migration,
    read_connection_identity,
)
from configs.maintenance.runtime_logging_storage import (
    configuration_rows,
    migrate_database,
    migrated_active_payload,
    migrated_payload,
    replace_configuration_rows,
)
from configs.migrations.gateway.v5_sqlite_to_v6_sqlite import prepare_v6_snapshot
from configs.migrations.workspace.v3_sqlite_to_v4_sqlite import prepare_v4_snapshot
from configs.runtime import (
    merge_json_objects,
    read_jsonc_object,
    validate_config_document_payload,
    validate_config_payload,
)


class MigrationAsset(TypedDict):
    kind: str
    path: str
    before: str
    after: str
    before_digest: str | None
    after_digest: str | None


class MigrationJournal(TypedDict):
    action: NotRequired[str]
    state: str
    workspaces: list[str]
    assets: list[MigrationAsset]
    backup_root: str
    settings: dict[str, str | bool] | None
    format_notice: str
    guards: list[str]
    databases: list[str]


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _database_digest(connection: sqlite3.Connection) -> str:
    return config_revision(cast(dict[str, object], configuration_rows(connection)))


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(sqlite_file_uri(path) + "?mode=rw", uri=True, isolation_level=None, timeout=0)
    connection.row_factory = sqlite3.Row
    return connection


def _lock_databases(paths: list[Path], stack: ExitStack) -> dict[Path, sqlite3.Connection]:
    connections: dict[Path, sqlite3.Connection] = {}
    for path in sorted(paths):
        ownership = SQLiteProcessOwnership(path)
        ownership.acquire()
        stack.callback(ownership.release)
        if not path.exists():
            continue
        connection = stack.enter_context(closing(_connection(path)))
        connection.execute("BEGIN IMMEDIATE")
        stack.callback(connection.rollback)
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='gateway_runtime_generation'").fetchone():
            for row in connection.execute(
                "SELECT process_id FROM gateway_runtime_generation WHERE state NOT IN ('closed','failed') AND process_id IS NOT NULL"
            ):
                if psutil.pid_exists(int(row[0])):
                    raise RuntimeError(f"Gateway 换代进程仍存活，必须先停止服务再迁移: pid={row[0]}")
        connections[path] = connection
    return connections


def _backup_database(path: Path, destination: Path) -> None:
    # 另一个读连接取得已提交的 WAL 快照；当前 owner 的写事务阻止并发写入。
    with (closing(sqlite3.connect(sqlite_file_uri(path) + "?mode=ro", uri=True)) as source,
          closing(sqlite3.connect(destination)) as target):
        source.backup(target)
    destination.chmod(0o600)


def validate_runtime_logging_plan(
    plan: RuntimeLoggingMigrationPlan, *, project_root: Path | None = None,
) -> None:
    """预检和发布共用正文转换，在停旧或创建备份前校验全部候选。"""
    inline = {
        domain: read_jsonc_object(resolve_config_resource_source(f"{domain}_inline.jsonc", project_root=project_root))
        for domain in ("gateway", "workspace")
    }
    connection_entries = list(next((cast(list[dict[str, object]], item.payload["entries"])
                                    for item in plan.inputs if item.kind == "connection_identity" and item.payload), []))
    files = {item.path: migrated_payload(item.payload or {}, item.domain, plan, connection_entries, source_key=item.key)
             for item in plan.inputs if item.kind == "file" and (
                 item.payload is not None or item.domain == "gateway" and item.key == "gateway.jsonc" and plan.settings is not None
             )}
    schemas = {domain: read_jsonc_object(resolve_config_resource_source(f"{domain}_schema.jsonc", project_root=project_root))
               for domain in ("gateway", "workspace")}
    gateway_snapshots = [item for item in plan.inputs if item.domain == "gateway"
                         and item.kind in {"active", "pending"} and item.payload is not None]
    gateway_layers_change = any(item.domain == "gateway" and item.kind in {"file", "source_layer"}
                                and item.config_version != 6 and
                                (item.payload is not None or item.kind == "source_layer") for item in plan.inputs)
    gateway_upgrades = {item.path for item in gateway_snapshots if item.config_version == 5}
    for item in gateway_snapshots:
        if item.path in gateway_upgrades:
            prepare_v6_snapshot(plan, item=item, project_root=project_root)
    for item in gateway_snapshots:
        if item.path in gateway_upgrades:
            continue
        if item.config_version != 6 or gateway_layers_change or plan.settings is not None:
            raise ValueError("Gateway 旧 active/pending 快照缺少可恢复的逐层来源文档，保留原件并阻断升级")
    for item in plan.inputs:
        if item.domain == "gateway" and item.kind == "source_layer" and item.payload is not None:
            candidate = migrated_payload(item.payload, "gateway", plan, connection_entries, source_key=item.key)
            try:
                jsonschema.validate(candidate, schemas["gateway"])
            except jsonschema.ValidationError as error:
                raise ValueError(f"配置验证失败: Gateway 来源层={item.identity}") from error
    for path, payload in files.items():
        if path.name.startswith("gateway"):
            try:
                jsonschema.validate(payload, schemas["gateway"])
            except jsonschema.ValidationError as error:
                raise ValueError(f"配置验证失败: Gateway 来源文件={path}") from error
    for item in plan.inputs:
        if item.kind != "file" or item.domain != "gateway" or item.payload is None or item.config_version == 6:
            continue
        # 旧层即使被当前显式父链绕过，也必须先证明转换后的业务字段有效。
        candidate = merge_json_objects(inline["gateway"], files[item.path], schema=schemas["gateway"])
        settings = cast(dict[str, object], candidate["settings"])
        validate_config_payload({
            **cast(dict[str, object], settings["gateway"]),
            "network": settings["network"], "config_version": 6,
        }, schema=schemas["gateway"])
    gateway_files = {item.key: item.path for item in plan.inputs if item.kind == "file" and item.domain == "gateway"}
    user = gateway_files["gateway.jsonc"]
    local = gateway_files["gateway_local.jsonc"]
    if user in files or local in files:
        from app.gateway.config.loader import load_gateway_config

        load_gateway_config(
            config_path=user, local_config_path=local,
            entry_path=local if local in files else user,
            inline_config_path=resolve_config_resource_source("gateway_inline.jsonc", project_root=project_root),
            schema_path=resolve_config_resource_source("gateway_schema.jsonc", project_root=project_root),
            source_overrides={
                **({"gateway_mutable_override": files[user]} if user in files else {}),
                **({"gateway_local_mutable_override": files[local]} if local in files else {}),
            },
            persist_migrations=False,
        )
    for item in plan.inputs:
        if item.domain == "workspace" and item.kind in {"file", "source_layer"} and item.payload is not None:
            candidate = migrated_payload(item.payload, item.domain, plan, source_key=item.key)
            validate_config_document_payload(candidate, schema=schemas["workspace"])
    if any(item.domain == "workspace" and item.config_version == 3 for item in plan.inputs):
        from app.services.infrastructure.config_service import ConfigService

        frozen = {}
        for path, payload in files.items():
            if not any(item.path == path and item.domain == "workspace" for item in plan.inputs):
                continue
            content = _json_bytes(payload)
            frozen[path] = StableConfigFile(path, "present", content, hashlib.sha256(content).hexdigest())
        user = next(item.path for item in plan.inputs if item.domain == "workspace" and item.kind == "file"
                    and item.key == "workspace.jsonc")
        for item in plan.inputs:
            if item.domain != "workspace" or item.kind != "file" or item.key != "workspace_root_mutable_override":
                continue
            read_workspace_id(item.path.parent.parent)
            service = ConfigService(config_path=user, workspace_root=item.path.parent.parent,
                                    inline_config_path=resolve_config_resource_source("workspace_inline.jsonc", project_root=project_root))
            service._schema = schemas["workspace"]
            service._build_candidate_snapshot(import_sources=False, frozen_files=frozen)
    workspace_upgrades = {path for domain, path in plan.databases if domain == "workspace"
                          and any(item.path == path and item.config_version == 3 for item in plan.inputs)}
    for item in plan.inputs:
        if item.path in workspace_upgrades and item.kind in {"active", "pending"}:
            prepare_v4_snapshot(plan, item=item, project_root=project_root)
    for item in plan.inputs:
        if item.kind in {"active", "pending"} and item.payload is not None:
            if item.path in workspace_upgrades or item.path in gateway_upgrades:
                continue
            candidate = migrated_active_payload(item.payload, item.domain, plan)
            if item.domain == "workspace":
                candidate = cast(dict[str, object], restore_environment_secret_references(candidate))
                validate_config_payload(candidate, schema=schemas["workspace"])
            else:
                validate_config_payload(candidate, schema=schemas["gateway"])


def _prepare(
    *, home: Path, plan: RuntimeLoggingMigrationPlan, workspaces: tuple[Path, ...],
    connections: dict[Path, sqlite3.Connection], project_root: Path | None,
) -> MigrationJournal:
    validate_runtime_logging_plan(plan, project_root=project_root)
    migration_root = home / "config/runtime-logging-migrations" / uuid4().hex
    migration_root.mkdir(parents=True, mode=0o700)
    assets: list[MigrationAsset] = []
    digests: dict[str, tuple[str | None, str | None]] = {}
    connection_entries: list[dict[str, object]] = []
    for domain, path in plan.databases:
        if domain == "gateway" and path in connections:
            record = read_connection_identity(connections[path])
            if record is not None:
                connection_entries = cast(list[dict[str, object]], record[1]["entries"])
    for index, item in enumerate(entry for entry in plan.inputs if entry.kind == "file"):
        original = read_stable_config_file(item.path)
        if original.digest != item.digest:
            raise RuntimeError(f"迁移预览后配置文件已变化: {item.path}")
        if item.payload is None and not (item.domain == "gateway" and item.key == "gateway.jsonc" and plan.settings is not None):
            continue
        payload = migrated_payload(item.payload or {}, item.domain, plan, connection_entries, source_key=item.key)
        content = _json_bytes(payload) if payload != item.payload or original.raw_bytes is None else original.raw_bytes
        assert content is not None
        before = migration_root / f"file-{index}.before"
        after = migration_root / f"file-{index}.after"
        if original.raw_bytes is not None:
            atomic_write(before, original.raw_bytes, 0o600)
        atomic_write(after, content, 0o600)
        digests[str(item.path)] = (original.digest, _digest(after))
        assets.append({"kind": "file", "path": str(item.path), "before": str(before), "after": str(after),
                       "before_digest": original.digest, "after_digest": _digest(after)})
    for index, (path, connection) in enumerate(connections.items()):
        domain: ConfigDomain = next(domain for domain, database in plan.databases if database == path)
        before, after = migration_root / f"db-{index}.before.sqlite", migration_root / f"db-{index}.after.sqlite"
        _backup_database(path, before)
        atomic_write(after, before.read_bytes(), 0o600)
        with closing(_connection(after)) as target:
            target.execute("BEGIN IMMEDIATE")
            migrate_database(
                target, domain=domain, source_path=path, plan=plan, file_digests=digests,
                connection_entries=connection_entries,
                project_root=project_root,
            )
            target.commit()
            after_digest = _database_digest(target)
        assets.append({"kind": "database", "path": str(path), "before": str(before), "after": str(after),
                       "before_digest": _database_digest(connection), "after_digest": after_digest})
    for domain in ("gateway", "workspace"):
        path = home / f"config/{domain}_schema.jsonc"
        before, after = migration_root / f"{domain}-schema.before", migration_root / f"{domain}-schema.after"
        if path.exists():
            atomic_write(before, path.read_bytes(), 0o600)
        atomic_write(after, resolve_config_resource_source(f"{domain}_schema.jsonc", project_root=project_root).read_bytes(), 0o600)
        assets.append({"kind": "file", "path": str(path), "before": str(before), "after": str(after),
                       "before_digest": _digest(path), "after_digest": _digest(after)})
    return {"state": "prepared", "workspaces": [str(path) for path in workspaces], "assets": assets,
            "backup_root": str(migration_root), "settings": plan.settings,
            "guards": list(dict.fromkeys([str(item.path) for item in plan.inputs if item.kind == "file"] + [str(path) for _, path in plan.databases])),
            "databases": [str(path) for _, path in plan.databases],
            "format_notice": "改动的 JSONC 已格式化；原始字节和 SQLite 原件保存在 backup_root。"}


def _publish(
    journal_path: Path, journal: MigrationJournal, connections: dict[Path, sqlite3.Connection], *, restore: bool,
    database_digest: Callable[[sqlite3.Connection], str] = _database_digest,
    replace_database: Callable[[sqlite3.Connection, sqlite3.Connection], None] = replace_configuration_rows,
) -> None:
    assets = journal["assets"]
    for path in [Path(path) for path in journal["guards"]] + [Path(asset["path"]) for asset in assets]:
        atomic_write(config_migration_marker(path), _json_bytes({"journal": str(journal_path)}), 0o600)
    # 全部 CAS 通过后才允许写第一个资产；中断重放只接受原版或计划结果。
    for asset in assets:
        path = Path(str(asset["path"]))
        observed = database_digest(connections[path]) if asset["kind"] == "database" else _digest(path)
        if observed not in (asset["before_digest"], asset["after_digest"]):
            raise RuntimeError(f"迁移期间配置被外部修改，拒绝覆盖；请核对备份: {path}")
        for backup, expected_digest in ((Path(asset["before"]), asset["before_digest"]), (Path(asset["after"]), asset["after_digest"])):
            if expected_digest is None:
                continue
            if asset["kind"] == "database":
                with closing(_connection(backup)) as source:
                    fingerprint = database_digest(source)
            else:
                fingerprint = _digest(backup)
            if fingerprint != expected_digest:
                raise RuntimeError(f"迁移备份校验失败: {backup}")
    journal["state"] = "restoring" if restore else "applying"
    atomic_write(journal_path, _json_bytes(journal), 0o600)
    for asset in reversed(assets) if restore else assets:
        path = Path(str(asset["path"]))
        content_path = Path(asset["before"] if restore else asset["after"])
        expected_digest = asset["before_digest"] if restore else asset["after_digest"]
        if asset["kind"] == "database":
            connection = connections[path]
            if database_digest(connection) != expected_digest:
                with closing(_connection(content_path)) as source:
                    replace_database(connection, source)
                connection.commit()
                connection.execute("BEGIN IMMEDIATE")
        elif _digest(path) != expected_digest:
            if expected_digest is None:
                path.unlink()
            else:
                atomic_write(path, content_path.read_bytes(), 0o600)
    journal["state"] = "restored" if restore else "completed"
    atomic_write(journal_path, _json_bytes(journal), 0o600)


def commit_runtime_logging_migration(
    *, boxteam_home: Path, workspaces: tuple[Path, ...] = (), level: str | None = None,
    pretty: bool | None = None, project_root: Path | None = None, gateway_sqlite_path: Path | None = None,
    restore: bool = False,
) -> dict[str, object]:
    home = boxteam_home.expanduser().resolve()
    workspaces = tuple(dict.fromkeys(path.expanduser().resolve() for path in workspaces))
    config_root = home / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    journal_path = config_root / ".runtime-logging-migration.json"
    lock_path = config_root / ".runtime-logging-migration.lock"
    with ExitStack() as stack:
        handle = stack.enter_context(lock_path.open("a+b"))
        portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        stack.callback(portalocker.unlock, handle)
        source_owner = home / "config/workspace-source.sqlite"
        if source_owner.exists():
            connection = stack.enter_context(closing(_connection(source_owner)))
            connection.execute("BEGIN IMMEDIATE")
            stack.callback(connection.rollback)
        journal = cast(MigrationJournal, json.loads(journal_path.read_text(encoding="utf-8"))) if journal_path.exists() else None
        if restore or journal is not None and journal["state"] not in {"completed", "restored"}:
            if journal is None:
                raise ValueError("没有可恢复的日志迁移原件")
            if workspaces and [str(path.expanduser().resolve()) for path in workspaces] != journal["workspaces"]:
                raise ValueError("中断迁移必须使用原工作区集合；省略工作区参数可继续原计划")
            settings = journal["settings"] or {}
            if (level is not None and level.strip().lower() != settings.get("level")) or (
                pretty is not None and pretty != settings.get("pretty")
            ):
                raise ValueError("中断迁移不能更换日志选择；先继续或 --restore 原计划")
            paths = [Path(path) for path in journal["databases"]]
            connections = _lock_databases(paths, stack)
        else:
            plan = preview_runtime_logging_migration(
                boxteam_home=home, workspaces=workspaces, level=level, pretty=pretty,
                project_root=project_root, gateway_sqlite_path=gateway_sqlite_path,
            )
            if plan.conflicts or plan.unsettled:
                raise ValueError("日志迁移有冲突或未结算配置；先用 --preview 查看并显式结算／选择")
            paths = [path for _, path in plan.databases]
            connections = _lock_databases(paths, stack)
            # 锁后重新核对候选、claim、来源层和 active，不能沿用锁外的预览。
            locked = preview_runtime_logging_migration(
                boxteam_home=home, workspaces=workspaces, level=level, pretty=pretty,
                project_root=project_root, gateway_sqlite_path=gateway_sqlite_path,
            )
            if locked != plan:
                raise RuntimeError("取得 owner 锁前配置状态已变化，请重新预览")
            journal = _prepare(home=home, plan=plan, workspaces=workspaces, connections=connections, project_root=project_root)
            if all(asset["before_digest"] == asset["after_digest"] for asset in journal["assets"]):
                shutil.rmtree(str(journal["backup_root"]))
                return {"state": "unchanged", "settings": plan.settings}
            atomic_write(journal_path, _json_bytes(journal), 0o600)
        _publish(journal_path, journal, connections, restore=restore)
        return {key: journal[key] for key in ("state", "backup_root", "settings", "format_notice")}
