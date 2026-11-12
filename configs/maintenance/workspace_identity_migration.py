"""工作区身份切换的只读预检；候选映射仅供显式迁移，不能用于运行时路由。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal, cast

import httpx

from app.core.config_sources import config_revision
from app.core.identifier import validate_gateway_id
from app.core.sqlite_state import sqlite_file_uri
from app.core.workspace_identity import validate_workspace_id
from app.gateway.control.gateway_registry.snapshot import (
    read_workspace_registry,
)
from app.gateway.control.generator_import import read_session_generator_inputs
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.federation import (
    RemoteGatewayConnection,
    discover_remote_gateway,
    start_remote_gateway_tunnel,
)
from app.gateway.runtime.workspace_process import resolve_backend_workspace_ids
from app.schemas.gateway_control import WorkspaceNavigationNodeDTO
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
class WorkspaceIdentityReference:
    source: str
    record_id: str
    legacy_workspace_id: str
    gateway_id: str
    workspace_id: str


@dataclass(frozen=True, slots=True)
class GeneratorImportPreflight:
    source: str
    task_id: str
    legacy_workspace_id: str
    source_hash: str | None
    status: Literal["pending", "imported", "invalid"]
    receipt_received: bool
    remaining_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RemoteIdentityProbe:
    connection: RemoteGatewayConnection
    # 保留原投影引用，不能以 UUID 或显示名猜测远端旧路由。
    workspace_routes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class WorkspaceIdentityPreview:
    gateway_id: str | None
    registry_source: str | None
    registry_revision: int | None
    registry_digest: str | None
    mappings: tuple[WorkspaceIdentityMapping, ...]
    blockers: tuple[IdentityMigrationBlocker, ...]
    references: tuple[WorkspaceIdentityReference, ...] = ()
    generator_imports: tuple[GeneratorImportPreflight, ...] = ()
    remote_probes: tuple[RemoteIdentityProbe, ...] = field(default=(), repr=False)

    def report(self) -> dict[str, object]:
        return {
            "validation_stage": "registry_identity_preflight", "read_only": True,
            "gateway_id": self.gateway_id, "registry_source": self.registry_source,
            "registry_revision": self.registry_revision, "registry_digest": self.registry_digest,
            "mapping_complete": not self.blockers and all(item.verified for item in self.mappings),
            "mappings": [asdict(item) for item in self.mappings],
            "blockers": [asdict(item) for item in self.blockers],
            "references": [asdict(item) for item in self.references],
            "generator_imports": [asdict(item) for item in self.generator_imports],
            "apply_available": self.registry_source is not None and not self.blockers and all(item.verified for item in self.mappings),
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


def _database_registry(path: Path) -> tuple[
    dict[str, object] | None, list[IdentityMigrationBlocker], tuple[tuple[str, str, str], ...],
]:
    if not path.exists():
        return None, [], ()
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
        # 视图主键与登记必须来自同一只读快照；不读取或改写其它视图业务字段。
        view_keys = tuple(
            (str(user_id), str(workspace_id), str(session_id))
            for user_id, workspace_id, session_id in connection.execute(
                "SELECT user_id,workspace_id,session_id FROM user_view_state ORDER BY user_id,workspace_id,session_id"
            )
        )
        return payload, blockers, view_keys


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


def _navigation_references(
    root: Path, mappings: tuple[WorkspaceIdentityMapping, ...],
) -> tuple[tuple[WorkspaceIdentityReference, ...], tuple[IdentityMigrationBlocker, ...]]:
    source = root / "navigation/workspace-tree.json"
    by_alias = {item.legacy_workspace_id: item for item in mappings}
    try:
        payload = _registry_file(source)
        if payload is None:
            return (), ()
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
            raise ValueError("身份迁移只接受 Gateway 导航格式 1")
        raw_nodes = _objects(payload, "nodes")
        nodes = []
        references = []
        for raw in raw_nodes:
            record = dict(raw)
            alias = record.pop("workspace_id", None)
            if record.get("kind") == "workspace_ref":
                if not isinstance(alias, str) or alias not in by_alias:
                    raise ValueError(f"导航引用未登记的工作区: {alias}")
                mapping = by_alias[alias]
                record["workspace"] = {"gateway_id": mapping.gateway_id, "workspace_id": mapping.workspace_id}
                references.append(WorkspaceIdentityReference(
                    "navigation/workspace-tree.json", _text(record, "node_id"), alias,
                    mapping.gateway_id, mapping.workspace_id,
                ))
            nodes.append(WorkspaceNavigationNodeDTO.model_validate(record))
        WorkspaceNavigationStore.validate_nodes(nodes, {(item.gateway_id, item.workspace_id) for item in mappings})
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
        return (), (IdentityMigrationBlocker("invalid_navigation_reference", str(source), str(error)),)
    return tuple(references), ()


def _user_view_references(
    keys: tuple[tuple[str, str, str], ...], mappings: tuple[WorkspaceIdentityMapping, ...],
) -> tuple[tuple[WorkspaceIdentityReference, ...], tuple[IdentityMigrationBlocker, ...]]:
    by_alias = {item.legacy_workspace_id: item for item in mappings}
    references = []
    blockers = []
    for user_id, workspace_id, session_id in keys:
        record_id = json.dumps((user_id, workspace_id, session_id), ensure_ascii=False, separators=(",", ":"))
        mapping = by_alias.get(workspace_id)
        if mapping is None:
            blockers.append(IdentityMigrationBlocker(
                "unknown_user_view_workspace_reference", f"user_view_state:{record_id}",
                f"用户视图工作区引用未登记: {workspace_id}；不能从当前活动项、名称或 Backend UUID 推定原归属",
            ))
            continue
        references.append(WorkspaceIdentityReference(
            "user_view_state.workspace_id", record_id, workspace_id, mapping.gateway_id, mapping.workspace_id,
        ))
    return tuple(references), tuple(blockers)


def _generator_import_preflight(
    root: Path,
) -> tuple[tuple[GeneratorImportPreflight, ...], tuple[IdentityMigrationBlocker, ...]]:
    records = []
    blockers = []
    for item in read_session_generator_inputs(root):
        source = item.source.relative_to(root).as_posix()
        status: Literal["pending", "imported", "invalid"] = (
            "invalid" if item.error is not None else "imported" if item.settled else "pending"
        )
        records.append(GeneratorImportPreflight(
            source, item.task_id, item.workspace_id,
            item.snapshot.request.source_hash if item.snapshot is not None else None,
            status, item.received, tuple(path.relative_to(root).as_posix() for path in item.remaining_sources),
        ))
        if item.error is not None:
            blockers.append(IdentityMigrationBlocker(
                "invalid_generator_import", source,
                f"旧 Generator 输入校验失败；{diagnostic_error(item.error, stage='generator_import_preflight')}；保留原件并由显式导入入口处理",
            ))
        elif not item.settled:
            reason = ("导入已有匹配回执，但运行源尚未全部退休" if item.received
                      else "冻结导入尚无匹配回执" if item.snapshot is not None else "旧 Generator 尚未显式导入")
            blockers.append(IdentityMigrationBlocker(
                "unsettled_generator_import", source,
                f"{reason}；先由既有显式导入入口结算，身份预检不会自动导入或修改冻结快照",
            ))
    return tuple(records), tuple(blockers)


def preview_workspace_identity_migration(
    *, gateway_root: Path, project_root: Path,
) -> WorkspaceIdentityPreview:
    """只读当前 Gateway 的明确注册根；不扫描工作区，不创建身份或启动目标。"""
    root = gateway_root.expanduser().resolve()
    file_payload = _registry_file(root / "workspaces.json")
    database_payload, blockers, view_keys = _database_registry(root / "gateway.sqlite")
    generator_imports, generator_blockers = _generator_import_preflight(root)
    blockers.extend(generator_blockers)
    # JSON 导入源与权威库同时存在时，不隐式选择较新的一个或覆盖冲突。
    if database_payload is not None and file_payload is not None and {
        key: value for key, value in database_payload.items() if key != "registry_revision"
    } != {key: value for key, value in file_payload.items() if key != "registry_revision"}:
        blockers.append(IdentityMigrationBlocker("registry_sources_conflict", str(root / "workspaces.json"), "JSON 注册表与 SQLite 权威快照不同"))
    payload = database_payload if database_payload is not None else file_payload
    if payload is None:
        references, navigation_blockers = _navigation_references(root, ())
        view_references, view_blockers = _user_view_references(view_keys, ())
        return WorkspaceIdentityPreview(
            None, None, None, None, (), (*blockers, *navigation_blockers, *view_blockers),
            (*references, *view_references), generator_imports,
        )
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
    remote_routes: dict[str, list[tuple[str, str]]] = {}
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
            if isinstance(target.get("remote_workspace_id"), str) and target["remote_workspace_id"]:
                remote_routes.setdefault(connection_id, []).append((identity, cast(str, target["remote_workspace_id"])))
            blockers.append(IdentityMigrationBlocker("remote_identity_unverified", identity, "远程来源尚需联邦入口核验；本地缓存不足以允许发布"))
        else:
            raise ValueError(f"工作区连接类型不在身份迁移支持范围: {kind}")
    source = root / ("gateway.sqlite" if database_payload is not None else "workspaces.json")
    references, navigation_blockers = _navigation_references(root, tuple(mappings))
    view_references, view_blockers = _user_view_references(view_keys, tuple(mappings))
    return WorkspaceIdentityPreview(
        gateway_id, str(source), cast(int | None, payload.get("registry_revision")),
        config_revision(payload), tuple(mappings), (*blockers, *navigation_blockers, *view_blockers),
        (*references, *view_references), generator_imports,
        tuple(RemoteIdentityProbe(RemoteGatewayConnection.from_registry_record(connections[key]), tuple(routes))
              for key, routes in remote_routes.items()),
    )


async def verify_remote_workspace_identities(
    preview: WorkspaceIdentityPreview, *, gateway_root: Path, log_dir: Path,
) -> WorkspaceIdentityPreview:
    """只核验冻结登记中的远端来源；诊断日志独立写入，不配对、启动或更新登记。"""
    credentials = FederationCredentialStore(storage_path=gateway_root.expanduser().resolve() / "credentials/federation.json")
    mappings = {item.legacy_workspace_id: replace(item, verified=False) if item.connection_id is not None else item
                for item in preview.mappings}
    blockers = [item for item in preview.blockers if item.code != "remote_identity_unverified"]
    for probe in preview.remote_probes:
        connection = probe.connection
        stage = "credentials"
        try:
            credential = credentials.get(connection.connection_id)
            if credential.peer_gateway_id != preview.gateway_id:
                raise ValueError("联邦凭据不属于当前登记 Gateway")
            stage = "tunnel"
            runtime = await start_remote_gateway_tunnel(connection=connection, log_dir=log_dir)
            try:
                stage = "discovery"
                _, items = await discover_remote_gateway(
                    gateway_url=runtime.service_urls["workspace_api"], credential=credential,
                    expected_remote_gateway_id=connection.remote_gateway_id,
                    verify_identity=True,
                )
                by_route = {str(item["workspace_id"]): item for item in items}
                if len(by_route) != len(items):
                    raise ValueError("远端原生工作区路由重复，无法确认来源")
            except BaseException as failure:
                try:
                    runtime.close()
                except BaseException as cleanup_error:
                    raise failure from cleanup_error
                raise
            else:
                runtime.close()
        except (OSError, RuntimeError, TypeError, ValueError, LookupError, httpx.HTTPError) as error:
            for alias, _ in probe.workspace_routes:
                blockers.append(IdentityMigrationBlocker(
                    "remote_identity_probe_failed", alias,
                    f"远端来源核验失败，保留原登记；{diagnostic_error(error, stage=f'remote_identity_{stage}')}",
                ))
            continue
        for alias, _ in probe.workspace_routes:
            mapping = mappings[alias]
            item = by_route.get(mapping.workspace_id)
            if item is None:
                blockers.append(IdentityMigrationBlocker("remote_workspace_missing", alias, "原工作区 UUID 未出现在已核验身份的原生 Gateway 列表中，不能重新绑定来源"))
            else:
                mappings[alias] = replace(mapping, verified=True)
    # 缺失原路由等不完整记录不能因其它连接核验通过而变成已确认。
    for mapping in mappings.values():
        if mapping.connection_id is not None and not mapping.verified and not any(
            item.source == mapping.legacy_workspace_id for item in blockers
        ):
            blockers.append(IdentityMigrationBlocker("remote_identity_unverified", mapping.legacy_workspace_id, "远端登记缺少可核验的原路由"))
    return replace(preview, mappings=tuple(mappings.values()), blockers=tuple(blockers))
