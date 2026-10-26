"""显式离线迁移：原件备份、逐资产 CAS 发布及中断后的继续／恢复。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from typing import TypedDict, cast
from uuid import uuid4

import portalocker
import psutil

from app.core.config_migration_guard import config_migration_marker
from app.core.config_sources import config_revision, read_stable_config_file
from app.core.sqlite_state import SQLiteProcessOwnership, sqlite_file_uri
from app.gateway.config.connection_ids import _CONNECTION_ID_RECORD_VERSION
from configs.installer import atomic_write, resolve_config_resource_source
from configs.runtime import merge_json_objects, read_jsonc_object, validate_config
from configs.runtime_logging_migration import (
    ConfigDomain,
    RuntimeLoggingMigrationPlan,
    preview_runtime_logging_migration,
)
from configs.runtime_logging_storage import (
    configuration_rows,
    migrate_database,
    migrated_payload,
    replace_configuration_rows,
)


class MigrationAsset(TypedDict):
    kind: str
    path: str
    before: str
    after: str
    before_digest: str | None
    after_digest: str | None


class MigrationJournal(TypedDict):
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


def _validate_outputs(
    plan: RuntimeLoggingMigrationPlan, files: dict[Path, dict[str, object]],
    databases: dict[Path, tuple[ConfigDomain, Path]], project_root: Path | None,
) -> None:
    effective = {
        domain: read_jsonc_object(resolve_config_resource_source(f"{domain}_inline.jsonc", project_root=project_root))
        for domain in ("gateway", "workspace")
    }
    for item in plan.inputs:
        if item.kind == "file" and item.path in files and item.path.parent.name != ".boxteam":
            effective[item.domain] = merge_json_objects(effective[item.domain], files[item.path])
    for domain in ("gateway", "workspace"):
        validate_config(effective[domain], schema_path=resolve_config_resource_source(f"{domain}_schema.jsonc", project_root=project_root))
    for path, payload in files.items():
        if path.name == "workspace.jsonc" and path.parent.name == ".boxteam":
            validate_config(merge_json_objects(effective["workspace"], payload), schema_path=resolve_config_resource_source("workspace_schema.jsonc", project_root=project_root))
    for domain, path in databases.values():
        with closing(_connection(path)) as connection:
            for row in connection.execute("SELECT payload_json FROM config_active_snapshot WHERE config_domain=?", (domain,)):
                validate_config(json.loads(row[0]), schema_path=resolve_config_resource_source(f"{domain}_schema.jsonc", project_root=project_root))


def _prepare(
    *, home: Path, plan: RuntimeLoggingMigrationPlan, workspaces: tuple[Path, ...],
    connections: dict[Path, sqlite3.Connection], project_root: Path | None,
) -> MigrationJournal:
    migration_root = home / "config/runtime-logging-migrations" / uuid4().hex
    migration_root.mkdir(parents=True, mode=0o700)
    assets: list[MigrationAsset] = []
    files: dict[Path, dict[str, object]] = {}
    digests: dict[str, tuple[str | None, str | None]] = {}
    connection_entries: list[dict[str, object]] = []
    for domain, path in plan.databases:
        if domain == "gateway" and path in connections:
            record = connections[path].execute("SELECT config_version,payload_json FROM gateway_config WHERE config_key='gateway_connection_ids'").fetchone()
            if record is not None:
                if record[0] != _CONNECTION_ID_RECORD_VERSION:
                    raise ValueError("Gateway connection_id 账本版本不受支持，必须先完成既有连接身份迁移")
                connection_entries = json.loads(record[1])["entries"]
    for index, item in enumerate(entry for entry in plan.inputs if entry.kind == "file"):
        original = read_stable_config_file(item.path)
        if original.digest != item.digest:
            raise RuntimeError(f"迁移预览后配置文件已变化: {item.path}")
        payload = migrated_payload(item.payload or {}, item.domain, plan, connection_entries)
        if item.payload is None and not (item.domain == "gateway" and item.key == "gateway.jsonc" and plan.settings is not None):
            continue
        files[item.path] = payload
        content = _json_bytes(payload) if payload != item.payload else original.raw_bytes
        assert content is not None
        before = migration_root / f"file-{index}.before"
        after = migration_root / f"file-{index}.after"
        if original.raw_bytes is not None:
            atomic_write(before, original.raw_bytes, 0o600)
        atomic_write(after, content, 0o600)
        digests[str(item.path)] = (original.digest, _digest(after))
        assets.append({"kind": "file", "path": str(item.path), "before": str(before), "after": str(after),
                       "before_digest": original.digest, "after_digest": _digest(after)})
    databases: dict[Path, tuple[ConfigDomain, Path]] = {}
    for index, (path, connection) in enumerate(connections.items()):
        domain: ConfigDomain = next(domain for domain, database in plan.databases if database == path)
        before, after = migration_root / f"db-{index}.before.sqlite", migration_root / f"db-{index}.after.sqlite"
        _backup_database(path, before)
        atomic_write(after, before.read_bytes(), 0o600)
        with closing(_connection(after)) as target:
            target.execute("BEGIN IMMEDIATE")
            migrate_database(target, domain=domain, source_path=path, plan=plan, file_digests=digests, connection_entries=connection_entries)
            target.commit()
            after_digest = _database_digest(target)
        databases[path] = (domain, after)
        assets.append({"kind": "database", "path": str(path), "before": str(before), "after": str(after),
                       "before_digest": _database_digest(connection), "after_digest": after_digest})
    _validate_outputs(plan, files, databases, project_root)
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
) -> None:
    assets = journal["assets"]
    for path in [Path(path) for path in journal["guards"]] + [Path(asset["path"]) for asset in assets]:
        atomic_write(config_migration_marker(path), _json_bytes({"journal": str(journal_path)}), 0o600)
    # 全部 CAS 通过后才允许写第一个资产；中断重放只接受原版或计划结果。
    for asset in assets:
        path = Path(str(asset["path"]))
        observed = _database_digest(connections[path]) if asset["kind"] == "database" else _digest(path)
        if observed not in (asset["before_digest"], asset["after_digest"]):
            raise RuntimeError(f"迁移期间配置被外部修改，拒绝覆盖；请核对备份: {path}")
        for backup, expected_digest in ((Path(asset["before"]), asset["before_digest"]), (Path(asset["after"]), asset["after_digest"])):
            if expected_digest is None:
                continue
            if asset["kind"] == "database":
                with closing(_connection(backup)) as source:
                    fingerprint = _database_digest(source)
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
            if _database_digest(connection) != expected_digest:
                with closing(_connection(content_path)) as source:
                    replace_configuration_rows(connection, source)
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
