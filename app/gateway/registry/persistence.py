from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, cast

from app.core.env import get_project_root
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
    ManagedBackendOwner,
)

from .core import _REGISTRY_SCHEMA_VERSION, WorkspaceTarget

if TYPE_CHECKING:
    from . import GatewayWorkspaceRegistry


class RegistryPersistenceMixin:
    def _load(self) -> None:
        state_payload = (
            self._state_store.load_workspace_registry()
            if self._state_store is not None
            else None
        )
        migrated_to_sqlite = state_payload is None and self._state_store is not None
        if state_payload is not None:
            payload = state_payload
            self._registry_revision = int(payload.get("registry_revision", 0))
        elif not self._storage_path.exists():
            return
        else:
            with self._storage_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        schema_version = payload.get("schema_version", 1)
        if not isinstance(schema_version, int) or schema_version < 1:
            raise ValueError(
                f"Gateway registry schema_version 必须是正整数: {self._storage_path}"
            )
        if schema_version < _REGISTRY_SCHEMA_VERSION:
            raise ValueError("Gateway registry 旧单工作区合同已失效；停止 writer 后显式清理旧开发数据")
        if schema_version > _REGISTRY_SCHEMA_VERSION:
            raise ValueError(
                "Gateway registry 版本高于当前程序支持范围: "
                f"version={schema_version}, supported={_REGISTRY_SCHEMA_VERSION}"
            )
        runtime_generation = payload.get("runtime_generation")
        if runtime_generation is not None and not isinstance(runtime_generation, str):
            raise ValueError("Gateway registry runtime_generation 必须是字符串或 null")
        self._runtime_generation = runtime_generation
        raw_remote_connections = payload.get("remote_gateway_connections", [])
        if not isinstance(raw_remote_connections, list):
            raise ValueError("Gateway registry remote_gateway_connections 必须是数组")  # noqa: TRY004 — 保留注册表格式校验既有拒绝类型。
        for item in raw_remote_connections:
            if not isinstance(item, dict):
                raise ValueError("Gateway registry 远程 Gateway 连接必须是对象")  # noqa: TRY004 — 保留注册表格式校验既有拒绝类型。
            source_owner = item.get("source_owner")
            if source_owner not in {"config", "manual"}:
                raise ValueError(
                    "Gateway registry 远程 Gateway 连接 source_owner 非法，"
                    "只接受 config 或 manual: "
                    f"connection_id={item.get('connection_id')}, "
                    f"source_owner={source_owner!r}"
                )
            remote_config_event_cursor = item.get("remote_config_event_cursor")
            if remote_config_event_cursor is not None and (
                isinstance(remote_config_event_cursor, bool)
                or not isinstance(remote_config_event_cursor, int)
                or remote_config_event_cursor < 0
            ):
                raise ValueError(
                    "Gateway registry 远程连接 remote_config_event_cursor 必须是非负整数或 null"
                )
            for field in ("remote_config_state", "remote_candidate_ref"):
                if item.get(field) is not None and not isinstance(item[field], str):
                    raise ValueError(
                        f"Gateway registry 远程连接 {field} 必须是字符串或 null"
                    )
            remote_restart_required = item.get("remote_restart_required", False)
            if not isinstance(remote_restart_required, bool):
                raise ValueError(
                    "Gateway registry 远程连接 remote_restart_required 必须是布尔值"
                )
            connection = RemoteGatewayConnection(
                connection_id=str(item["connection_id"]),
                name=str(item["name"]),
                host=str(item["host"]),
                port=int(item["port"]),
                username=str(item["username"]),
                private_key_path=(
                    str(item["private_key_path"])
                    if item.get("private_key_path") is not None
                    else None
                ),
                ssh_config_host=(
                    str(item["ssh_config_host"])
                    if item.get("ssh_config_host") is not None
                    else None
                ),
                remote_gateway_port=int(item["remote_gateway_port"]),
                remote_gateway_id=str(item["remote_gateway_id"]),
                protocol_version=int(item["protocol_version"]),
                connection_error=(
                    str(item["connection_error"])
                    if item.get("connection_error") is not None
                    else None
                ),
                remote_pair_command=(
                    str(item["remote_pair_command"])
                    if item.get("remote_pair_command") is not None
                    else None
                ),
                source_owner=source_owner,
                remote_config_event_cursor=remote_config_event_cursor,
                remote_config_state=item.get("remote_config_state"),
                remote_restart_required=remote_restart_required,
                remote_candidate_ref=item.get("remote_candidate_ref"),
            )
            self._remote_gateway_connections[connection.connection_id] = connection
        targets = payload.get("targets", [])
        if not isinstance(targets, list):
            raise ValueError(
                f"Gateway registry targets 必须是数组: {self._storage_path}"
            )
        persisted_active_id = payload.get("active_workspace_id")
        parent_workspace_ids: dict[str, str | None] = {}
        for item in targets:
            if not isinstance(item, dict):
                raise ValueError(f"Gateway registry target 必须是对象: {self._storage_path}")  # noqa: TRY004 — 保留注册表格式校验既有拒绝类型。
            original_workspace_id = str(item["workspace_id"])
            root_path = item.get("root_path")
            backend_url = str(item["backend_url"])
            connection_kind = item["connection_kind"]
            if connection_kind == "ssh":
                raise ValueError(
                    "检测到旧 SSH 直连后端注册记录。该模式已移除；请删除旧远程"
                    "工作区并重新添加“远程 Gateway 连接”。旧字段 "
                    "remote_backend_host/remote_backend_port/remote_services "
                    "不能自动迁移。"
                )
            if connection_kind not in {"local", "remote_gateway"}:
                raise ValueError(
                    "Gateway registry connection_kind 非法: "
                    f"workspace_id={original_workspace_id}, kind={connection_kind}"
                )
            for field in ("owner", "target_namespace", "connection_id", "target_generation"):
                if field not in item:
                    raise ValueError(
                        f"Gateway registry target 缺少 {field}: workspace_id={original_workspace_id}"
                    )
            target_generation = item["target_generation"]
            if not isinstance(target_generation, str) or not target_generation.strip():
                raise ValueError(
                    f"Gateway registry target_generation 必须是非空字符串: workspace_id={original_workspace_id}"
                )
            managed = bool(item.get("managed", False))
            system_default = bool(item.get("system_default", False))
            desired_running = item.get("desired_running", False)
            if not isinstance(desired_running, bool):
                raise ValueError(  # noqa: TRY004 — 保留持久化格式既有拒绝类型。
                    f"Gateway registry desired_running 必须是布尔值: workspace_id={original_workspace_id}"
                )
            workspace_id = original_workspace_id
            if workspace_id in self._targets:
                raise ValueError(f"Gateway registry 工作区 ID 重复: {workspace_id}")
            raw_parent_workspace_id = item.get("parent_workspace_id")
            if raw_parent_workspace_id is not None and not isinstance(
                raw_parent_workspace_id,
                str,
            ):
                raise ValueError(
                    "Gateway registry parent_workspace_id 必须是字符串或 null: "
                    f"workspace_id={original_workspace_id}"
                )
            parent_workspace_ids[workspace_id] = raw_parent_workspace_id
            raw_local_service_urls = item.get("local_service_urls", {})
            if not isinstance(raw_local_service_urls, dict) or not all(
                isinstance(name, str) and isinstance(url, str)
                for name, url in raw_local_service_urls.items()
            ):
                raise ValueError(
                    "Gateway registry local_service_urls 必须是字符串映射: "
                    f"workspace_id={original_workspace_id}"
                )
            raw_backend_workspace_id = item.get("backend_workspace_id")
            if raw_backend_workspace_id is not None and not isinstance(
                raw_backend_workspace_id, str
            ):
                raise ValueError(
                    "Gateway registry backend_workspace_id 必须是字符串或 null: "
                    f"workspace_id={original_workspace_id}"
                )
            target = WorkspaceTarget(
                workspace_id=workspace_id,
                name=str(item["name"]),
                root_path=root_path,
                backend_url=backend_url,
                connection_kind=connection_kind,
                owner=item["owner"],
                target_namespace=item["target_namespace"],
                connection_id=item["connection_id"],
                target_generation=target_generation,
                runtime_lease_id=(
                    str(item["runtime_lease_id"])
                    if item.get("runtime_lease_id") is not None
                    else None
                ),
                active_request_count=int(item.get("active_request_count", 0)),
                active_stream_count=int(item.get("active_stream_count", 0)),
                name_customized=bool(item.get("name_customized", False)),
                managed=managed,
                removable=bool(item.get("removable", True)),
                system_default=system_default,
                desired_running=desired_running,
                remote_gateway_connection_id=(
                    str(item["remote_gateway_connection_id"])
                    if item.get("remote_gateway_connection_id") is not None
                    else None
                ),
                remote_workspace_id=(
                    str(item["remote_workspace_id"])
                    if item.get("remote_workspace_id") is not None
                    else None
                ),
                remote_config_event_cursor=(
                    int(item["remote_config_event_cursor"])
                    if item.get("remote_config_event_cursor") is not None
                    else None
                ),
                remote_service_names=tuple(item.get("remote_service_names", ())),
                remote_backend_generation=item.get("remote_backend_generation"),
                remote_affected_workspace_ids=tuple(item.get("remote_affected_workspace_ids", ())),
                local_service_urls={
                    str(name): str(url)
                    for name, url in raw_local_service_urls.items()
                },
                connection_error=(
                    str(item["connection_error"])
                    if item.get("connection_error") is not None
                    else None
                ),
                backend_workspace_id=(
                    raw_backend_workspace_id
                ),
            )
            self._validate_target_identity(target)
            if target.connection_kind == "remote_gateway":
                if (
                    target.remote_gateway_connection_id is None
                    or target.remote_workspace_id is None
                ):
                    raise ValueError(
                        "远程投影工作区缺少 remote_gateway_connection_id 或 "
                        f"remote_workspace_id: {target.workspace_id}"
                    )
                if (
                    target.remote_gateway_connection_id
                    not in self._remote_gateway_connections
                ):
                    raise ValueError(
                        "远程投影工作区引用未知 Gateway 连接: "
                        f"{target.remote_gateway_connection_id}"
                    )
            self._targets[target.workspace_id] = target
        for workspace_id, raw_parent_workspace_id in parent_workspace_ids.items():
            if raw_parent_workspace_id is None:
                continue
            parent_workspace_id = raw_parent_workspace_id
            if parent_workspace_id not in self._targets:
                raise ValueError(
                    "Gateway registry 父工作区不存在: "
                    f"workspace_id={workspace_id}, parent_workspace_id={parent_workspace_id}"
                )
            self._targets[workspace_id].parent_workspace_id = parent_workspace_id
        raw_owners = payload.get("managed_backends")
        if not isinstance(raw_owners, list):
            raise TypeError("Gateway registry 缺少 managed_backends 完整合同数组")
        seen_members: set[str] = set()
        for item in raw_owners:
            if not isinstance(item, dict) or set(item) != {"owner_id", "generation", "members", "backend_url", "debug_port"} or not isinstance(item["members"], list):
                raise TypeError("managed Backend owner 必须为完整合同对象")
            member_payloads = item["members"]
            for member in member_payloads:
                if not isinstance(member, dict) or set(member) != {"workspace_id", "backend_workspace_id"}:
                    raise TypeError("managed Backend member 只能包含 Gateway ID 与 Backend UUID")
            members = tuple(
                ManagedBackendMember(member["workspace_id"], member["backend_workspace_id"])
                for member in member_payloads
            )
            contract = ManagedBackendContract(item["owner_id"], item["generation"], members, item["backend_url"], item["debug_port"])
            if contract.owner_id in self._backend_owners or seen_members.intersection(contract.workspace_ids):
                raise ValueError("managed Backend owner 或成员重复")
            for member in members:
                target = self.resolve(member.workspace_id)
                if not target.managed or target.connection_kind != "local" or target.backend_workspace_id != member.backend_workspace_id:
                    raise ValueError("持久化 Backend 完整合同与成员 UUID 不匹配")
            cast("GatewayWorkspaceRegistry", self).resolve_backend_mounts(contract)
            if len({self.resolve(workspace_id).desired_running for workspace_id in contract.workspace_ids}) != 1:
                raise ValueError("同一 Backend 完整组启动意图不一致")
            seen_members.update(contract.workspace_ids)
            self._backend_owners[contract.owner_id] = ManagedBackendOwner(contract)
        self._validate_parent_graph()
        migrated_active_id = persisted_active_id if isinstance(persisted_active_id, str) else None
        if migrated_active_id is not None and migrated_active_id in self._targets:
            self._active_workspace_id = migrated_active_id
        elif self._targets:
            self._active_workspace_id = next(iter(self._targets))
        self._order_customized = bool(payload.get("order_customized", False))
        if migrated_to_sqlite:
            cast("GatewayWorkspaceRegistry", self).validate_local_root_bindings(get_project_root())
            if migrated_to_sqlite and self._storage_path.is_file():
                backup_path = self._storage_path.with_name(
                    f"{self._storage_path.name}.migrated.bak"
                )
                if not backup_path.exists():
                    shutil.copy2(self._storage_path, backup_path)
            self._save()

    def _save(
        self,
        *,
        owner: str = "registry",
        remote_gateway_connection_ids: tuple[str, ...] = (),
        targets: dict[str, WorkspaceTarget] | None = None,
        backend_contracts: tuple[ManagedBackendContract, ...] | None = None,
        publish: Callable[[], None] | None = None,
    ) -> None:
        previous_state = getattr(self, "_last_committed_snapshot", None)
        selected_targets = targets if targets is not None else self._targets
        selected_active = self._active_workspace_id
        if selected_active not in selected_targets:
            selected_active = next(
                (target.workspace_id for target in selected_targets.values() if target.system_default),
                next(iter(selected_targets), None),
            )
        payload = {
            "schema_version": _REGISTRY_SCHEMA_VERSION,
            "active_workspace_id": selected_active,
            "order_customized": self._order_customized,
            "runtime_generation": self._runtime_generation,
            "managed_backends": [
                contract.to_payload() for contract in (
                    backend_contracts if backend_contracts is not None
                    else tuple(owner.contract for owner in self._backend_owners.values())
                )
            ],
            "remote_gateway_connections": [
                asdict(connection)
                for connection in self._remote_gateway_connections.values()
            ],
            "targets": [
                {key: value for key, value in asdict(target).items() if key != "root_path" or target.connection_kind == "local"}
                for target in selected_targets.values()
            ],
        }
        if self._state_store is not None:
            try:
                self._registry_revision = self._state_store.replace_workspace_registry(
                    payload,
                    expected_revision=self._registry_revision,
                    owner=owner,
                    remote_gateway_connection_ids=remote_gateway_connection_ids,
                )
            except Exception:
                if publish is None and previous_state is not None:
                    self._restore_registry_state(previous_state)
                raise
        else:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self._storage_path.name}.",
                dir=self._storage_path.parent,
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                    json.dump(payload, file, ensure_ascii=False, indent=2)
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
                # TODO: Windows 使用继承 ACL；不要把 POSIX mode bits 当作安全边界。
                if os.name != "nt":
                    temporary_path.chmod(0o600)
                os.replace(temporary_path, self._storage_path)
            finally:
                temporary_path.unlink(missing_ok=True)
        # Backend 候选在持久化成功后整体公布，旧 target/URL 映射不接受原位补丁。
        if publish is not None:
            publish()
        self._active_workspace_id = selected_active
        self._last_committed_snapshot = self._capture_registry_state()
        self._notify_commit_observers()
    def _capture_registry_state(self) -> dict[str, object]:
        return {
            "targets": deepcopy(self._targets),
            "backend_contracts": {owner_id: owner.contract for owner_id, owner in self._backend_owners.items()},
            "active_workspace_id": self._active_workspace_id,
            "order_customized": self._order_customized,
            "remote_gateway_connections": deepcopy(self._remote_gateway_connections),
            "registry_revision": self._registry_revision,
            "runtime_generation": self._runtime_generation,
        }
    def _restore_registry_state(self, snapshot: dict[str, object]) -> None:
        self._targets = deepcopy(snapshot["targets"])
        contracts = snapshot["backend_contracts"]
        for owner_id in tuple(self._backend_owners):
            if owner_id not in contracts:
                self._backend_owners.pop(owner_id)
        for owner_id, contract in contracts.items():
            if owner_id in self._backend_owners:
                self._backend_owners[owner_id].contract = contract
            else:
                self._backend_owners[owner_id] = ManagedBackendOwner(contract)
        self._active_workspace_id = snapshot["active_workspace_id"]
        self._order_customized = bool(snapshot["order_customized"])
        self._remote_gateway_connections = deepcopy(
            snapshot["remote_gateway_connections"]
        )
        self._registry_revision = int(snapshot["registry_revision"])
        runtime_generation = snapshot.get("runtime_generation")
        if runtime_generation is not None and not isinstance(runtime_generation, str):
            raise TypeError("Gateway registry runtime_generation 必须是字符串或 null")
        self._runtime_generation = runtime_generation
        self._route_signatures = {
            workspace_id: self._route_signature(target)
            for workspace_id, target in self._targets.items()
        }
    def add_commit_observer(self, observer: Callable[[], None]) -> None:
        """注册 registry commit 观察者；每次成功持久化后按注册顺序回调。"""

        self._commit_observers.append(observer)
    def remove_commit_observer(self, observer: Callable[[], None]) -> None:
        """owner 停止时移除原回调，不让关闭后的 owner 接收提交。"""
        if observer in self._commit_observers:
            self._commit_observers.remove(observer)
    def _notify_commit_observers(self) -> None:
        """在单点提交后通知观察者；端口重投影失败必须响亮失败，不得静默。"""

        for observer in self._commit_observers:
            observer()
