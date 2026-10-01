from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from app.gateway.federation import RemoteGatewayConnection

from .core import _REGISTRY_SCHEMA_VERSION, WorkspaceTarget


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
            raise ValueError("Gateway registry remote_gateway_connections 必须是数组")
        for item in raw_remote_connections:
            if not isinstance(item, dict):
                raise ValueError("Gateway registry 远程 Gateway 连接必须是对象")
            source_owner = item.get("source_owner")
            if source_owner not in {"config", "manual"}:
                raise ValueError(
                    "Gateway registry 远程 Gateway 连接 source_owner 非法，"
                    "只接受 config 或 manual: "
                    f"connection_id={item.get('connection_id')}, "
                    f"source_owner={source_owner!r}"
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
            )
            self._remote_gateway_connections[connection.connection_id] = connection
        targets = payload.get("targets", [])
        if not isinstance(targets, list):
            raise ValueError(f"Gateway registry targets 必须是数组: {self._storage_path}")
        persisted_active_id = payload.get("active_workspace_id")
        workspace_id_remap: dict[str, str] = {}
        parent_workspace_ids: dict[str, str | None] = {}
        migrated = schema_version < _REGISTRY_SCHEMA_VERSION
        for item in targets:
            if not isinstance(item, dict):
                raise ValueError(f"Gateway registry target 必须是对象: {self._storage_path}")
            original_workspace_id = str(item["workspace_id"])
            root_path = str(item["root_path"])
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
            managed = bool(item.get("managed", False))
            system_default = bool(item.get("system_default", False))
            if schema_version >= 9:
                desired_running_value = item.get("desired_running", False)
                if not isinstance(desired_running_value, bool):
                    raise ValueError(
                        "Gateway registry desired_running 必须是布尔值: "
                        f"workspace_id={original_workspace_id}"
                    )
                desired_running = desired_running_value
            else:
                # TODO: schema<9 没有记录显式启动意图，只能可靠迁移当前激活的
                # 本地托管工作区；完成一次性迁移后移除此兼容分支。
                desired_running = (
                    connection_kind == "local"
                    and managed
                    and original_workspace_id == persisted_active_id
                )
            # TODO: 旧 12 位 Gateway ID 完成一次性迁移后，在下一个持久化格式大版本移除。
            workspace_id = self._migrate_legacy_workspace_id(
                workspace_id=original_workspace_id,
                root_path=root_path,
                backend_url=backend_url,
                connection_kind=connection_kind,
                managed=managed,
                system_default=system_default,
                schema_version=schema_version,
            )
            if workspace_id in self._targets:
                raise ValueError(
                    "Gateway registry 工作区 ID 迁移后发生冲突: "
                    f"original={original_workspace_id}, migrated={workspace_id}"
                )
            workspace_id_remap[original_workspace_id] = workspace_id
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
            migrated = migrated or workspace_id != original_workspace_id
            raw_local_service_urls = item.get("local_service_urls", {})
            if not isinstance(raw_local_service_urls, dict) or not all(
                isinstance(name, str) and isinstance(url, str)
                for name, url in raw_local_service_urls.items()
            ):
                raise ValueError(
                    "Gateway registry local_service_urls 必须是字符串映射: "
                    f"workspace_id={original_workspace_id}"
                )
            target = WorkspaceTarget(
                workspace_id=workspace_id,
                name=str(item["name"]),
                root_path=root_path,
                backend_url=backend_url,
                connection_kind=connection_kind,
                owner=(
                    item.get("owner")
                    if item.get("owner") in {
                        "config",
                        "manual",
                        "system",
                        "remote_projection",
                    }
                    else (
                        "remote_projection"
                        if connection_kind == "remote_gateway"
                        else "system"
                        if system_default
                        else "manual"
                    )
                ),
                target_namespace=str(item.get("target_namespace", "gateway")),
                connection_id=(
                    str(item["connection_id"])
                    if item.get("connection_id") is not None
                    else (
                        str(item["remote_gateway_connection_id"])
                        if connection_kind == "remote_gateway"
                        and item.get("remote_gateway_connection_id") is not None
                        else None
                    )
                ),
                target_generation=str(
                    item.get("target_generation") or "target_generation_legacy"
                ),
                runtime_lease_id=(
                    str(item["runtime_lease_id"])
                    if item.get("runtime_lease_id") is not None
                    else None
                ),
                active_request_count=int(item.get("active_request_count", 0)),
                active_stream_count=int(item.get("active_stream_count", 0)),
                # TODO: 所有 schema<4 的 Registry 完成一次性迁移后，在下一个持久化格式大版本移除默认值。
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
                local_service_urls={
                    str(name): str(url)
                    for name, url in raw_local_service_urls.items()
                },
                connection_error=(
                    str(item["connection_error"])
                    if item.get("connection_error") is not None
                    else None
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
            parent_workspace_id = workspace_id_remap.get(
                raw_parent_workspace_id,
                raw_parent_workspace_id,
            )
            if parent_workspace_id not in self._targets:
                raise ValueError(
                    "Gateway registry 父工作区不存在: "
                    f"workspace_id={workspace_id}, parent_workspace_id={parent_workspace_id}"
                )
            self._targets[workspace_id].parent_workspace_id = parent_workspace_id
        self._validate_parent_graph()
        migrated_active_id = (
            workspace_id_remap.get(persisted_active_id, persisted_active_id)
            if isinstance(persisted_active_id, str)
            else None
        )
        if migrated_active_id is not None and migrated_active_id in self._targets:
            self._active_workspace_id = migrated_active_id
        elif self._targets:
            self._active_workspace_id = next(iter(self._targets))
        self._order_customized = bool(payload.get("order_customized", False))
        if migrated or migrated_to_sqlite:
            if migrated_to_sqlite and self._storage_path.is_file():
                backup_path = self._storage_path.with_name(
                    f"{self._storage_path.name}.migrated.bak"
                )
                if not backup_path.exists():
                    shutil.copy2(self._storage_path, backup_path)
            self._save()
    def _save(self, *, owner: str = "registry") -> None:
        previous_state = getattr(self, "_last_committed_snapshot", None)
        payload = {
            "schema_version": _REGISTRY_SCHEMA_VERSION,
            "active_workspace_id": self._active_workspace_id,
            "order_customized": self._order_customized,
            "runtime_generation": self._runtime_generation,
            "remote_gateway_connections": [
                asdict(connection)
                for connection in self._remote_gateway_connections.values()
            ],
            "targets": [asdict(target) for target in self._targets.values()],
        }
        if self._state_store is not None:
            try:
                self._registry_revision = self._state_store.replace_workspace_registry(
                    payload,
                    expected_revision=self._registry_revision,
                    owner=owner,
                )
            except Exception:
                if previous_state is not None:
                    self._restore_registry_state(previous_state)
                raise
            self._last_committed_snapshot = self._capture_registry_state()
            self._notify_commit_observers()
            return
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
        self._last_committed_snapshot = self._capture_registry_state()
        self._notify_commit_observers()
    def _capture_registry_state(self) -> dict[str, object]:
        return {
            "targets": deepcopy(self._targets),
            "active_workspace_id": self._active_workspace_id,
            "order_customized": self._order_customized,
            "remote_gateway_connections": deepcopy(self._remote_gateway_connections),
            "registry_revision": self._registry_revision,
            "runtime_generation": self._runtime_generation,
        }
    def _restore_registry_state(self, snapshot: dict[str, object]) -> None:
        self._targets = deepcopy(snapshot["targets"])
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
    def _notify_commit_observers(self) -> None:
        """在单点提交后通知观察者；端口重投影失败必须响亮失败，不得静默。"""

        for observer in self._commit_observers:
            observer()
