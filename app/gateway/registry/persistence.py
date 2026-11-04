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
from app.core.file_permissions import restrict_private_path_access
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
    ManagedBackendOwner,
)

from .core import (
    _REGISTRY_SCHEMA_VERSION,
    WorkspaceKey,
    WorkspaceTarget,
    workspace_owner_key,
    workspace_owner_payload,
)

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
            raise ValueError("Gateway registry 身份合同需要显式迁移；停止 writer 后运行 workspace-identity 维护入口，禁止清空旧数据")
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
            connection = RemoteGatewayConnection.from_registry_record(item)
            self._remote_gateway_connections[connection.connection_id] = connection
        targets = payload.get("targets", [])
        if not isinstance(targets, list):
            raise ValueError(
                f"Gateway registry targets 必须是数组: {self._storage_path}"
            )
        if payload.get("gateway_id") != self.gateway_id:
            raise ValueError("Gateway registry 原生 Gateway 身份与当前 identity.json 不一致")
        persisted_active = workspace_owner_key(payload.get("active_workspace"))
        for item in targets:
            if not isinstance(item, dict):
                raise TypeError("Gateway registry target 必须是对象")
            record = dict(item)
            record["parent_workspace"] = workspace_owner_key(record.get("parent_workspace"))
            for field in ("remote_gateway_connection_ids", "remote_service_names", "remote_affected_workspace_ids"):
                value = record.get(field, [])
                if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
                    raise TypeError(f"Gateway registry {field} 必须是字符串数组")
                record[field] = tuple(value)
            record.setdefault("root_path", None)
            target = WorkspaceTarget(**record)
            self._validate_target_identity(target)
            if not target.target_generation:
                raise ValueError(f"Gateway registry target_generation 缺失: {target.key}")
            if target.key in self._targets:
                raise ValueError(f"Gateway registry 工作区 owner 重复: {target.key}")
            for connection_id in target.remote_gateway_connection_ids:
                connection = self.remote_gateway_connection(connection_id)
                if connection.remote_gateway_id != target.gateway_id:
                    raise ValueError(f"Gateway registry 连接归属不匹配: {target.key}, {connection_id}")
            self._targets[target.key] = target
        for target in self._targets.values():
            if target.parent_workspace is not None and target.parent_workspace not in self._targets:
                raise ValueError(f"Gateway registry 父工作区不存在: {target.key}, parent={target.parent_workspace}")
        raw_owners = payload.get("managed_backends")
        if not isinstance(raw_owners, list):
            raise TypeError("Gateway registry 缺少 managed_backends 完整合同数组")
        seen_members: set[str] = set()
        for item in raw_owners:
            if not isinstance(item, dict) or set(item) != {"gateway_id", "owner_id", "generation", "members", "backend_url", "debug_port"} or not isinstance(item["members"], list):
                raise TypeError("managed Backend owner 必须为完整合同对象")
            member_payloads = item["members"]
            for member in member_payloads:
                if not isinstance(member, dict) or set(member) != {"workspace_id"}:
                    raise TypeError("managed Backend member 只能包含工作区 UUID")
            members = tuple(
                ManagedBackendMember(workspace_id=member["workspace_id"])
                for member in member_payloads
            )
            contract = ManagedBackendContract(gateway_id=item["gateway_id"], owner_id=item["owner_id"], generation=item["generation"], members=members, backend_url=item["backend_url"], debug_port=item["debug_port"])
            if contract.gateway_id != self.gateway_id:
                raise ValueError("managed Backend 不能属于另一个原生 Gateway")
            if contract.owner_id in self._backend_owners or seen_members.intersection(contract.workspace_ids):
                raise ValueError("managed Backend owner 或成员重复")
            for member in members:
                target = self.resolve(member.workspace_id)
                if not target.managed or target.connection_kind != "local":
                    raise ValueError("持久化 Backend 完整合同与成员 UUID 不匹配")
            cast("GatewayWorkspaceRegistry", self).resolve_backend_mounts(contract)
            if len({self.resolve(workspace_id).desired_running for workspace_id in contract.workspace_ids}) != 1:
                raise ValueError("同一 Backend 完整组启动意图不一致")
            seen_members.update(contract.workspace_ids)
            self._backend_owners[contract.owner_id] = ManagedBackendOwner(contract)
        self._validate_parent_graph()
        if persisted_active is not None and persisted_active not in self._targets:
            raise ValueError(f"Gateway registry 活动工作区不存在: {persisted_active}")
        self._active_workspace = persisted_active
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
        targets: dict[WorkspaceKey, WorkspaceTarget] | None = None,
        backend_contracts: tuple[ManagedBackendContract, ...] | None = None,
        publish: Callable[[], None] | None = None,
    ) -> None:
        previous_state = getattr(self, "_last_committed_snapshot", None)
        selected_targets = targets if targets is not None else self._targets
        selected_active = self._active_workspace
        if selected_active not in selected_targets:
            selected_active = next(
                (target.key for target in selected_targets.values() if target.system_default),
                next(iter(selected_targets), None),
            )
        payload = {
            "schema_version": _REGISTRY_SCHEMA_VERSION,
            "gateway_id": self.gateway_id,
            "active_workspace": workspace_owner_payload(selected_active),
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
                {
                    **{
                        key: list(value) if isinstance(value, tuple) else value
                        for key, value in asdict(target).items()
                        if key != "root_path" or target.connection_kind == "local"
                    },
                    "parent_workspace": workspace_owner_payload(target.parent_workspace),
                }
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
                    restrict_private_path_access(
                        temporary_path,
                        descriptor=descriptor,
                    )
                    json.dump(payload, file, ensure_ascii=False, indent=2)
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary_path, self._storage_path)
            finally:
                temporary_path.unlink(missing_ok=True)
        # Backend 候选在持久化成功后整体公布，旧 target/URL 映射不接受原位补丁。
        if publish is not None:
            publish()
        self._active_workspace = selected_active
        self._last_committed_snapshot = self._capture_registry_state()
        self._notify_commit_observers()
    def _capture_registry_state(self) -> dict[str, object]:
        return {
            "targets": deepcopy(self._targets),
            "backend_contracts": {owner_id: owner.contract for owner_id, owner in self._backend_owners.items()},
            "active_workspace": self._active_workspace,
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
        self._active_workspace = snapshot["active_workspace"]
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
