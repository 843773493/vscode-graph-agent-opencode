"""工作区身份切换的只读预检；候选映射仅供显式迁移，不能用于运行时路由。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from app.core.config_sources import config_revision
from app.core.identifier import validate_gateway_id
from app.core.sqlite_state import sqlite_file_uri
from app.core.workspace_identity import validate_workspace_id
from app.gateway.control.gateway_registry.snapshot import (
    read_workspace_registry,
)
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.runtime.workspace_process import resolve_backend_workspace_ids
from configs.diagnostics import diagnostic_error


@dataclass(frozen=True, slots=True)
class WorkspaceIdentityMapping:
    registered_gateway_id: str
    legacy_workspace_id: str
    gateway_id: str
    workspace_id: str
    connection_id: str | None
    verified: bool


@dataclass(frozen=True, slots=True)
class IdentityMigrationBlocker:
    code: str
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class WorkspaceIdentityPreview:
    gateway_id: str | None
    registry_source: str | None
    registry_revision: int | None
    registry_digest: str | None
    mappings: tuple[WorkspaceIdentityMapping, ...]
    blockers: tuple[IdentityMigrationBlocker, ...]

    def report(self) -> dict[str, object]:
        return {
            "validation_stage": "registry_identity_preflight", "read_only": True,
            "gateway_id": self.gateway_id, "registry_source": self.registry_source,
            "registry_revision": self.registry_revision, "registry_digest": self.registry_digest,
            "mapping_complete": not self.blockers and all(item.verified for item in self.mappings),
            "mappings": [asdict(item) for item in self.mappings],
            "blockers": [asdict(item) for item in self.blockers],
            # TODO(unify-workspace-identity 6.1/6.2)：迁齐其余引用并实现可恢复原子发布后才能提供 apply。
            "apply_available": False,
            "remaining_checks": ["persistent_reference_migration", "client_recovery_material", "recoverable_atomic_publish"],
        }


def _objects(payload: dict[str, object], key: str) -> list[dict[str, object]]:
    value = payload.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"Gateway 身份预检要求 {key} 为对象数组")
    return cast(list[dict[str, object]], value)


def _text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Gateway 身份预检缺少有效 {key}")
    return value


def _registry_file(path: Path) -> dict[str, object] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError(f"Gateway registry 必须是 JSON 对象: {path}")
    return value


def _unsettled(connection: sqlite3.Connection) -> list[IdentityMigrationBlocker]:
    states = (
        ("config_active_snapshot", "config_domain", "state", "state <> 'active'"),
        ("config_pending_candidate", "candidate_id", "state", "state NOT IN ('active','rejected','discarded')"),
        ("config_apply_journal", "apply_id", "state", "state IN ('applying','recovery_required')"),
        ("registry_apply_journal", "apply_id", "state", "state IN ('applying','recovery_required')"),
        ("gateway_restart_intent", "intent_id", "state", "state IN ('pending','applying','recovery_required')"),
        ("gateway_runtime_generation", "generation_id", "state", "state NOT IN ('failed','closed') OR listener_state <> 'closed'"),
    )
    result = [
        IdentityMigrationBlocker("unsettled_control_state", f"{table}:{identity}", f"状态尚未结算: {state}")
        for table, key, state_column, condition in states
        for identity, state in connection.execute(f"SELECT {key},{state_column} FROM {table} WHERE {condition} ORDER BY {key}")
    ]
    result.extend(
        IdentityMigrationBlocker("unsettled_control_state", f"config_apply_claim:{identity}", "配置 apply 租约仍存在，必须先由原 owner 结算")
        for identity, in connection.execute("SELECT apply_id FROM config_apply_claim ORDER BY apply_id")
    )
    return result


def _database_registry(path: Path) -> tuple[dict[str, object] | None, list[IdentityMigrationBlocker]]:
    if not path.exists():
        return None, []
    with closing(sqlite3.connect(sqlite_file_uri(path) + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        version, = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        if version != 18:
            raise ValueError(f"工作区身份预检只支持 Gateway SQLite schema 18，实际为 {version}")
        payload = read_workspace_registry(connection)
        blockers = _unsettled(connection)
        for key, version, raw in connection.execute(
            "SELECT config_key,config_version,payload_json FROM gateway_config WHERE config_key='gateway_connection_ids'"
        ):
            if version != 4:
                blockers.append(IdentityMigrationBlocker("unsupported_connection_record", key, f"先完成既有 connection_id 升级，实际版本: {version}"))
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise TypeError("gateway_connection_ids 必须是对象")
            migration = record.get("migration")
            if isinstance(migration, dict) and migration.get("state") == "planned":
                blockers.append(IdentityMigrationBlocker("unsettled_connection_migration", key, "连接身份迁移尚未完成"))
        if payload is not None:
            revision = connection.execute("SELECT revision FROM registry_meta WHERE registry_key='workspace'").fetchone()
            if revision is None or revision[0] != payload["registry_revision"]:
                blockers.append(IdentityMigrationBlocker("registry_revision_mismatch", str(path), "registry revision 与 metadata 不一致"))
            rows = connection.execute("SELECT workspace_id,active,payload_json FROM gateway_workspace_registry ORDER BY position,workspace_id").fetchall()
            for identity, _, raw in rows:
                if identity != json.loads(raw).get("workspace_id"):
                    blockers.append(IdentityMigrationBlocker("registry_key_mismatch", str(identity), "SQLite 行主键与工作区 payload 不一致"))
            active = [identity for identity, flag, _ in rows if flag]
            if active != ([] if payload["active_workspace_id"] is None else [payload["active_workspace_id"]]):
                blockers.append(IdentityMigrationBlocker("active_selection_mismatch", str(path), "SQLite 活动标记与 registry metadata 不一致"))
        return payload, blockers


def _references(payload: dict[str, object], targets: dict[str, dict[str, object]]) -> list[IdentityMigrationBlocker]:
    blockers: list[IdentityMigrationBlocker] = []
    active = payload.get("active_workspace_id")
    if active is not None and active not in targets:
        blockers.append(IdentityMigrationBlocker("unknown_workspace_reference", "active_workspace_id", f"未知工作区: {active}"))
    for identity, target in targets.items():
        visited = {identity}
        parent = target.get("parent_workspace_id")
        while parent is not None:
            if not isinstance(parent, str) or parent not in targets:
                blockers.append(IdentityMigrationBlocker("unknown_workspace_reference", identity, f"未知父工作区: {parent}"))
                break
            if parent in visited:
                blockers.append(IdentityMigrationBlocker("workspace_parent_cycle", identity, "工作区父链形成循环"))
                break
            visited.add(parent)
            parent = targets[parent].get("parent_workspace_id")
    members: set[str] = set()
    for backend in _objects(payload, "managed_backends"):
        owner = _text(backend, "owner_id")
        for member in _objects(backend, "members"):
            identity = _text(member, "workspace_id")
            target = targets.get(identity)
            if target is None or target.get("connection_kind") != "local" or not target.get("managed"):
                blockers.append(IdentityMigrationBlocker("invalid_backend_member", owner, f"成员不是已登记的本地 managed 工作区: {identity}"))
            elif target.get("backend_workspace_id") != member.get("backend_workspace_id"):
                blockers.append(IdentityMigrationBlocker("backend_identity_mismatch", owner, f"成员 UUID 与 root binding 不一致: {identity}"))
            if identity in members:
                blockers.append(IdentityMigrationBlocker("duplicate_backend_member", owner, f"成员重复归属: {identity}"))
            members.add(identity)
    for identity, target in targets.items():
        if target.get("managed") and target.get("connection_kind") == "local" and identity not in members:
            blockers.append(IdentityMigrationBlocker("missing_backend_member", identity, "本地 managed 工作区缺少唯一 Backend owner"))
        if target.get("active_request_count", 0) or target.get("active_stream_count", 0):
            blockers.append(IdentityMigrationBlocker("active_route_references", identity, "注册记录仍有活动请求或流，必须先排空"))
    return blockers


def preview_workspace_identity_migration(
    *, gateway_root: Path, project_root: Path,
) -> WorkspaceIdentityPreview:
    """只读当前 Gateway 的明确注册根；不扫描工作区，不创建身份或启动目标。"""
    root = gateway_root.expanduser().resolve()
    file_payload = _registry_file(root / "workspaces.json")
    database_payload, blockers = _database_registry(root / "gateway.sqlite")
    # JSON 导入源与权威库同时存在时，不隐式选择较新的一个或覆盖冲突。
    if database_payload is not None and file_payload is not None and {
        key: value for key, value in database_payload.items() if key != "registry_revision"
    } != {key: value for key, value in file_payload.items() if key != "registry_revision"}:
        blockers.append(IdentityMigrationBlocker("registry_sources_conflict", str(root / "workspaces.json"), "JSON 注册表与 SQLite 权威快照不同"))
    payload = database_payload if database_payload is not None else file_payload
    if payload is None:
        return WorkspaceIdentityPreview(None, None, None, None, (), tuple(blockers))
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 13:
        raise ValueError("工作区身份预检只支持 registry 合同 13；先完成既有版本升级")
    gateway_id = load_or_create_gateway_id(root / "identity.json", read_only=True)
    target_list = _objects(payload, "targets")
    targets = {_text(item, "workspace_id"): item for item in target_list}
    if len(targets) != len(target_list):
        raise ValueError("Gateway registry 工作区别名重复")
    blockers.extend(_references(payload, targets))
    connections = {_text(item, "connection_id"): item for item in _objects(payload, "remote_gateway_connections")}
    if len(connections) != len(_objects(payload, "remote_gateway_connections")):
        raise ValueError("Gateway registry 连接身份重复")
    locals_by_root: dict[Path, list[str]] = {}
    for identity, target in targets.items():
        if target.get("connection_kind") == "local":
            native_root = Path(_text(target, "root_path"))
            if not native_root.is_absolute():
                raise ValueError(f"本地 root binding 必须是绝对路径: {identity}")
            locals_by_root.setdefault(native_root.resolve(), []).append(identity)
    observed: dict[Path, str] = {}
    if locals_by_root:
        try:
            observed = dict(zip(locals_by_root, resolve_backend_workspace_ids(
                project_root, tuple(locals_by_root), initialize=False,
            ), strict=True))
        except (OSError, RuntimeError, TypeError, ValueError, subprocess.TimeoutExpired) as error:
            blockers.append(IdentityMigrationBlocker("local_identity_probe_failed", "workspace_identity_cli", diagnostic_error(error, stage="read_only_identity")))
    mappings: list[WorkspaceIdentityMapping] = []
    local_owners: dict[str, str] = {}
    for identity, target in targets.items():
        kind = target.get("connection_kind")
        stored_id = target.get("backend_workspace_id")
        if stored_id is None and kind == "local":
            stored_id = observed.get(Path(_text(target, "root_path")).resolve())
        if stored_id is None:
            blockers.append(IdentityMigrationBlocker("missing_workspace_identity", identity, "登记与只读目标均不能确认工作区 UUID"))
            continue
        if not isinstance(stored_id, str):
            raise TypeError("工作区登记 UUID 必须是字符串")
        workspace_id = validate_workspace_id(stored_id)
        if kind == "local":
            native_root = Path(_text(target, "root_path")).resolve()
            verified = observed.get(native_root) == workspace_id
            if native_root in observed and not verified:
                blockers.append(IdentityMigrationBlocker("workspace_identity_mismatch", identity, f"登记 UUID={workspace_id}，目标 UUID={observed[native_root]}"))
            if workspace_id in local_owners:
                blockers.append(IdentityMigrationBlocker("duplicate_native_workspace", identity, f"UUID 已被另一 root binding 登记: {local_owners[workspace_id]}"))
            local_owners[workspace_id] = identity
            mappings.append(WorkspaceIdentityMapping(gateway_id, identity, gateway_id, workspace_id, None, verified))
        elif kind == "remote_gateway":
            connection_id = _text(target, "remote_gateway_connection_id")
            if target.get("connection_id") != connection_id or not target.get("remote_workspace_id"):
                blockers.append(IdentityMigrationBlocker("invalid_remote_reference", identity, "远程工作区连接关联或来源引用不完整"))
            connection = connections.get(connection_id)
            if connection is None:
                blockers.append(IdentityMigrationBlocker("missing_remote_connection", identity, f"远程连接不存在: {connection_id}"))
                continue
            owner = validate_gateway_id(_text(connection, "remote_gateway_id"))
            mappings.append(WorkspaceIdentityMapping(gateway_id, identity, owner, workspace_id, connection_id, False))
            # TODO(unify-workspace-identity 4.4/6.1)：通过原联邦 manifest 核验来源，不能把缓存声明当成已确认身份。
            blockers.append(IdentityMigrationBlocker("remote_identity_unverified", identity, "远程来源尚需联邦入口核验；本地缓存不足以允许发布"))
        else:
            raise ValueError(f"工作区连接类型不在身份迁移支持范围: {kind}")
    source = root / ("gateway.sqlite" if database_payload is not None else "workspaces.json")
    return WorkspaceIdentityPreview(
        gateway_id, str(source), cast(int | None, payload.get("registry_revision")),
        config_revision(payload), tuple(mappings), tuple(blockers),
    )
