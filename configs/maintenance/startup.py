"""在服务开放写入前调用原迁移 owner，不在普通 loader 中兼容旧格式。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

import psutil

from app.core.logging_contract import load_runtime_log_contract
from app.core.sqlite_state import sqlite_file_uri
from app.gateway.control.gateway_registry.snapshot import read_workspace_registry
from app.gateway.control.gateway_state import _GATEWAY_MIGRATIONS
from app.services.infrastructure.external_resource_leases.database import (
    retire_empty_legacy_ledger,
)
from app.services.infrastructure.workspace_state_store import _WORKSPACE_MIGRATIONS
from configs.maintenance.runtime_logging_commit import (
    commit_runtime_logging_migration,
    validate_runtime_logging_plan,
)
from configs.maintenance.runtime_logging_migration import (
    preview_runtime_logging_migration,
)
from configs.maintenance.workspace_identity_commit import (
    commit_workspace_identity_migration,
)
from configs.maintenance.workspace_identity_migration import (
    preview_workspace_identity_migration,
    verify_remote_workspace_identities,
)


def _journal_state(path: Path) -> str | None:
    state = json.loads(path.read_text(encoding="utf-8"))["state"] if path.exists() else None
    if state not in {None, "prepared", "applying", "restoring", "completed", "restored"}:
        raise ValueError(f"迁移 journal 状态未知，保留原件: {path}")
    if state == "restored":
        raise RuntimeError("原迁移已显式回退；须由用户选择旧程序或重新执行原迁移，不自动反转恢复方向")
    return state


def _registry(root: Path) -> dict[str, object] | None:
    database = root / "gateway.sqlite"
    if database.exists():
        with closing(sqlite3.connect(sqlite_file_uri(database) + "?mode=ro", uri=True)) as connection:
            # 新空库没有登记；存量登记只由原 owner 解释，不扫描磁盘补身份。
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='gateway_config'").fetchone():
                stored = read_workspace_registry(connection)
                if stored is not None:
                    if stored.get("schema_version") not in {13, 14}:
                        raise ValueError("工作区登记版本未知，保留原件；需要用户决定支持的升级来源")
                    return stored
    source = root / "workspaces.json"
    stored = json.loads(source.read_text(encoding="utf-8")) if source.exists() else None
    if stored is not None and stored.get("schema_version") not in {13, 14}:
        raise ValueError("工作区登记版本未知，保留原件；需要用户决定支持的升级来源")
    return stored


def _sqlite_upgrade(path: Path, target: int) -> dict[str, object] | None:
    """只读原 owner 的有序账本，不初始化库或执行迁移。"""
    if not path.exists():
        return None
    with closing(sqlite3.connect(sqlite_file_uri(path) + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone():
            raise ValueError(f"SQLite 迁移来源未知，缺少有序账本；保留原件: {path}")
        applied = tuple(row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version"))
    if any(type(version) is not int or version < 1 for version in applied):
        raise ValueError(f"SQLite 迁移账本版本未知；保留原件: path={path}, applied={applied}")
    current = applied[-1] if applied else 0
    if current > target:
        raise ValueError(f"SQLite 迁移账本版本未知；保留原件: path={path}, current={current}, supported={target}")
    if applied != tuple(range(1, current + 1)):
        raise ValueError(f"SQLite 迁移账本缺号；保留原件: path={path}, applied={applied}")
    return {"path": str(path), "current_schema_version": current, "target_schema_version": target} if current < target else None


def prepare_startup_migrations(
    *, boxteam_home: Path, project_root: Path,
    gateway_root: Path, workspaces: tuple[Path, ...] = (),
    preview: bool = False,
    planned_files: Mapping[Path, bytes] | None = None,
) -> dict[str, object]:
    """受支持来源自动升级；冲突、未知版本和写锁失败保留原件并阻断启动。"""
    reports: dict[str, object] = {}
    logging_state = _journal_state(boxteam_home / "config/.runtime-logging-migration.json")
    identity_state = _journal_state(gateway_root / ".workspace-identity-migration.json")
    if preview and (logging_state not in {None, "completed"} or identity_state not in {None, "completed"}):
        return {"recovery": {"state": "migration_required"}}
    if logging_state not in {None, "completed"}:
        # 原冻结计划包含工作区集合；恢复原方向，不从部分发布的登记重建计划。
        reports["configuration"] = commit_runtime_logging_migration(
            boxteam_home=boxteam_home, project_root=project_root,
            gateway_sqlite_path=gateway_root / "gateway.sqlite", restore=logging_state == "restoring",
        )
        if logging_state == "restoring":
            raise RuntimeError("已完成原配置回退；保留恢复方向，本次不启动新版本服务")
    if identity_state not in {None, "completed"}:
        reports["workspace_identity"] = commit_workspace_identity_migration(
            gateway_root=gateway_root, project_root=project_root, restore=identity_state == "restoring",
        )
        if identity_state == "restoring":
            raise RuntimeError("已完成原身份回退；保留恢复方向，本次不启动新版本服务")
    registry = _registry(gateway_root)
    roots = list(workspaces)
    if registry is not None:
        targets = registry["targets"]
        if not isinstance(targets, list):
            raise TypeError("Gateway registry targets 必须是数组")
        roots.extend(Path(item["root_path"]) for item in cast(list[dict[str, str]], targets)
                     if item.get("connection_kind") == "local")
    roots = list(dict.fromkeys(path.expanduser().resolve() for path in roots))
    empty_resource_roots = [root for root in roots if retire_empty_legacy_ledger(root, preview=True)]
    databases = (
        (gateway_root / "gateway.sqlite", len(_GATEWAY_MIGRATIONS)),
        *((root / ".boxteam/state/workspace.sqlite", len(_WORKSPACE_MIGRATIONS)) for root in roots),
    )
    sql_upgrades = [upgrade for path, target in databases if (upgrade := _sqlite_upgrade(path, target)) is not None]
    if sql_upgrades:
        reports["sqlite"] = {
            "state": "migration_required" if preview else "pending_owner_startup",
            "databases": sql_upgrades,
        }
    plan = preview_runtime_logging_migration(
        boxteam_home=boxteam_home, project_root=project_root, workspaces=tuple(roots),
        gateway_sqlite_path=gateway_root / "gateway.sqlite", planned_files=planned_files,
    )
    identity = None
    if registry is not None and registry["schema_version"] == 13:
        identity = preview_workspace_identity_migration(gateway_root=gateway_root, project_root=project_root)
        # 仅只读预览允许存活运行代沿原停旧流程结算；崩溃残留不能推迟到配置发布后才拒绝。
        live_generations: set[str] = set()
        if preview and (gateway_root / "gateway.sqlite").exists():
            with closing(sqlite3.connect(sqlite_file_uri(gateway_root / "gateway.sqlite") + "?mode=ro", uri=True)) as connection:
                live_generations = {
                    f"gateway_runtime_generation:{generation}"
                    for generation, pid in connection.execute(
                        "SELECT generation_id,process_id FROM gateway_runtime_generation "
                        "WHERE state NOT IN ('closed','failed') AND process_id IS NOT NULL"
                    ) if psutil.pid_exists(pid)
                }
        connection_upgrades = {item.key for item in plan.inputs if item.kind == "connection_identity" and item.config_version == 3}
        identity = replace(identity, blockers=tuple(
            item for item in identity.blockers
            if not (item.code == "unsupported_connection_record" and item.source in connection_upgrades)
            and not (item.code == "unsettled_control_state" and item.source in live_generations)
            and not (item.code == "active_route_references" and live_generations)
        ))
        identity = asyncio.run(verify_remote_workspace_identities(
            identity, gateway_root=gateway_root,
            log_dir=load_runtime_log_contract(project_root).root_for(boxteam_home),
        ))
        if identity.blockers or not all(item.verified for item in identity.mappings):
            raise ValueError("身份迁移存在冲突或未结算来源，原件未改写: " + ", ".join(item.code for item in identity.blockers))
    if "configuration" not in reports:
        needs_upgrade = any(
            item.payload is not None and (
                item.config_version != item.target_version
                or item.domain == "workspace" and "logger" in item.payload
                or item.domain == "workspace" and isinstance(item.payload.get("runtime"), dict)
                and "auxiliary_services" in item.payload["runtime"]
                or item.domain == "gateway" and isinstance(item.payload.get("features"), dict)
                and "session_generators" in item.payload["features"]
            ) for item in plan.inputs
        )
        # 升级器在停旧前做只读预检；存活运行代仍由原停止流程结算，写入时必须重新取得 ownership。
        blockers = tuple(item for item in plan.unsettled if not (preview and item["kind"] == "runtime"))
        if needs_upgrade or plan.recoveries or blockers:
            if plan.conflicts or blockers:
                raise ValueError(
                    "自动升级需要用户决定配置取舍或结算未完成操作；请运行 "
                    "configs.boxteam migrate-runtime-config --preview 查看原来源，原件未改写"
                )
            validate_runtime_logging_plan(plan, project_root=project_root)
            reports["configuration"] = {"state": "migration_required"} if preview else commit_runtime_logging_migration(
                boxteam_home=boxteam_home, project_root=project_root, workspaces=tuple(roots),
                gateway_sqlite_path=gateway_root / "gateway.sqlite",
            )
    if identity is not None:
        if preview:
            reports["workspace_identity"] = {"state": "migration_required"}
        else:
            reports["workspace_identity"] = commit_workspace_identity_migration(
                gateway_root=gateway_root, project_root=project_root, verified_preview=identity,
            )
    if not preview and ("configuration" in reports or "workspace_identity" in reports):
        # 前序 owner 可能已推进 SQL 账本；只报告服务启动时仍待执行的部分。
        remaining = [upgrade for path, target in databases if (upgrade := _sqlite_upgrade(path, target)) is not None]
        if remaining:
            reports["sqlite"] = {"state": "pending_owner_startup", "databases": remaining}
        else:
            reports.pop("sqlite", None)
    if empty_resource_roots:
        if not preview:
            for root in empty_resource_roots:
                retire_empty_legacy_ledger(root)
        reports["empty_resource_ledgers"] = {
            "state": "migration_required" if preview else "completed",
            "workspaces": [str(root) for root in empty_resource_roots],
        }
    return reports
