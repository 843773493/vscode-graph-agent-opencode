from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import httpx

from app.core.path_utils import get_gateway_root
from app.gateway.credentials import FederationCredentialStore
from app.gateway.schemas import (
    GatewayConfigReloadStatusDTO,
    GatewayConnectionKind,
    GatewayRemoteConnectionSummaryDTO,
    GatewayServiceStatus,
    GatewayServiceStatusDTO,
    GatewayWorkspaceDTO,
)
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.service_types import GatewayServiceName
from app.gateway.workspace_ids import (
    build_managed_local_workspace_id,
    build_workspace_id,
    is_legacy_workspace_id,
)

_REGISTRY_SCHEMA_VERSION = 7
_UNSET = object()


@dataclass(slots=True)
class WorkspaceTarget:
    workspace_id: str
    name: str
    root_path: str
    backend_url: str
    connection_kind: GatewayConnectionKind
    parent_workspace_id: str | None = None
    name_customized: bool = False
    managed: bool = False
    removable: bool = True
    system_default: bool = False
    remote_gateway_connection_id: str | None = None
    remote_workspace_id: str | None = None
    remote_service_names: tuple[GatewayServiceName, ...] = ()
    connection_error: str | None = None


class GatewayWorkspaceRegistry:
    def __init__(self, *, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._targets: dict[str, WorkspaceTarget] = {}
        self._active_workspace_id: str | None = None
        self._order_customized = False
        self._runtimes: dict[str, WorkspaceRuntime] = {}
        self._remote_gateway_connections: dict[str, RemoteGatewayConnection] = {}
        self._remote_gateway_runtimes: dict[str, WorkspaceRuntime] = {}
        self._load()

    @property
    def active_workspace_id(self) -> str | None:
        return self._active_workspace_id

    def close(self) -> None:
        errors: list[str] = []
        for workspace_id, runtime in list(self._runtimes.items()):
            try:
                runtime.close()
            except Exception as error:
                errors.append(f"{workspace_id}: {error}")
        self._runtimes.clear()
        for connection_id, runtime in list(self._remote_gateway_runtimes.items()):
            try:
                runtime.close()
            except Exception as error:
                errors.append(f"{connection_id}: {error}")
        self._remote_gateway_runtimes.clear()
        if errors:
            raise RuntimeError("关闭 Gateway 托管进程失败: " + "; ".join(errors))

    def upsert(
        self,
        target: WorkspaceTarget,
        *,
        runtime: WorkspaceRuntime | None = None,
        activate: bool = True,
    ) -> WorkspaceTarget:
        self._targets[target.workspace_id] = target
        if runtime is not None:
            previous = self._runtimes.pop(target.workspace_id, None)
            if previous is not None:
                previous.close()
            self._runtimes[target.workspace_id] = runtime
        if activate or self._active_workspace_id is None:
            self._active_workspace_id = target.workspace_id
        self._save()
        return target

    def managed_runtime(self, workspace_id: str) -> WorkspaceRuntime:
        target = self.resolve(workspace_id)
        if not target.managed:
            raise ValueError(f"工作区不由 Gateway 托管: {workspace_id}")
        runtime = self._runtimes.get(workspace_id)
        if runtime is None:
            raise RuntimeError(f"托管工作区缺少运行时: {workspace_id}")
        return runtime

    def remove(self, workspace_id: str) -> None:
        target = self._targets.get(workspace_id)
        if target is None:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        if not target.removable or target.system_default:
            raise PermissionError(f"默认工作区不能删除: {target.name}")
        runtime = self._runtimes.pop(workspace_id, None)
        if runtime is not None:
            runtime.close()
        for child in self._targets.values():
            if child.parent_workspace_id == workspace_id:
                child.parent_workspace_id = None
        del self._targets[workspace_id]
        self._close_unused_remote_gateway(target.remote_gateway_connection_id)
        if self._active_workspace_id == workspace_id:
            self._active_workspace_id = self._default_workspace_id()
        self._save()

    def remove_backend_aliases(self, *, backend_url: str, keep_workspace_id: str) -> None:
        normalized_backend_url = backend_url.rstrip("/")
        changed = False
        for workspace_id, target in list(self._targets.items()):
            if workspace_id == keep_workspace_id:
                continue
            if target.backend_url.rstrip("/") != normalized_backend_url:
                continue
            runtime = self._runtimes.pop(workspace_id, None)
            if runtime is not None:
                runtime.close()
            del self._targets[workspace_id]
            changed = True
        if self._active_workspace_id not in self._targets:
            self._active_workspace_id = self._default_workspace_id()
            changed = True
        if changed:
            self._save()

    def remove_system_default_aliases(self, *, keep_workspace_id: str) -> None:
        changed = False
        for workspace_id, target in list(self._targets.items()):
            if workspace_id == keep_workspace_id or not target.system_default:
                continue
            runtime = self._runtimes.pop(workspace_id, None)
            if runtime is not None:
                runtime.close()
            del self._targets[workspace_id]
            changed = True
        if self._active_workspace_id not in self._targets:
            self._active_workspace_id = keep_workspace_id
            changed = True
        if changed:
            self._save()

    def ensure_default_workspace_first(self) -> None:
        if self._order_customized:
            return
        default_workspace_id = self._default_workspace_id()
        if default_workspace_id is None:
            return
        first_workspace_id = next(iter(self._targets), None)
        if first_workspace_id == default_workspace_id:
            return
        self._targets = {
            default_workspace_id: self._targets[default_workspace_id],
            **{
                workspace_id: target
                for workspace_id, target in self._targets.items()
                if workspace_id != default_workspace_id
            },
        }
        self._save()

    def reorder(self, workspace_ids: list[str]) -> None:
        if len(workspace_ids) != len(set(workspace_ids)):
            raise ValueError("Gateway 工作区排序列表包含重复 ID")
        known_workspace_ids = set(self._targets)
        requested_workspace_ids = set(workspace_ids)
        unknown_workspace_ids = sorted(requested_workspace_ids - known_workspace_ids)
        missing_workspace_ids = sorted(known_workspace_ids - requested_workspace_ids)
        if unknown_workspace_ids:
            raise ValueError(f"Gateway 工作区排序包含未知 ID: {', '.join(unknown_workspace_ids)}")
        if missing_workspace_ids:
            raise ValueError(f"Gateway 工作区排序缺少 ID: {', '.join(missing_workspace_ids)}")
        self._targets = {
            workspace_id: self._targets[workspace_id]
            for workspace_id in workspace_ids
        }
        self._order_customized = True
        self._save()

    def activate(self, workspace_id: str) -> None:
        if workspace_id not in self._targets:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        self._active_workspace_id = workspace_id
        self._save()

    def rename(self, workspace_id: str, name: str) -> WorkspaceTarget:
        return self.update(workspace_id, name=name)

    def set_parent(
        self,
        workspace_id: str,
        parent_workspace_id: str | None,
    ) -> WorkspaceTarget:
        return self.update(
            workspace_id,
            parent_workspace_id=parent_workspace_id,
        )

    def update(
        self,
        workspace_id: str,
        *,
        name: str | object = _UNSET,
        parent_workspace_id: str | None | object = _UNSET,
    ) -> WorkspaceTarget:
        target = self._targets.get(workspace_id)
        if target is None:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")

        normalized_name: str | None = None
        if name is not _UNSET:
            if not isinstance(name, str):
                raise TypeError("Gateway 工作区名称必须是字符串")
            normalized_name = name.strip()
            if not normalized_name:
                raise ValueError("Gateway 工作区名称不能为空")

        normalized_parent_workspace_id: str | None = None
        if parent_workspace_id is not _UNSET:
            if parent_workspace_id is not None and not isinstance(
                parent_workspace_id,
                str,
            ):
                raise TypeError("Gateway 父工作区 ID 必须是字符串或 null")
            normalized_parent_workspace_id = parent_workspace_id
            if normalized_parent_workspace_id == workspace_id:
                raise ValueError("工作区不能成为自己的父工作区")
            if (
                normalized_parent_workspace_id is not None
                and normalized_parent_workspace_id not in self._targets
            ):
                raise KeyError(
                    f"未知 Gateway 父工作区: {normalized_parent_workspace_id}"
                )
            ancestor_id = normalized_parent_workspace_id
            while ancestor_id is not None:
                if ancestor_id == workspace_id:
                    raise ValueError("工作区父子关系不能形成循环")
                ancestor_id = self._targets[ancestor_id].parent_workspace_id

        if normalized_name is not None:
            target.name = normalized_name
            target.name_customized = True
        if parent_workspace_id is not _UNSET:
            target.parent_workspace_id = normalized_parent_workspace_id
        self._save()
        return target

    def resolve(self, workspace_id: str | None = None) -> WorkspaceTarget:
        target_id = workspace_id or self._active_workspace_id
        if target_id is None:
            raise LookupError("Gateway 尚未注册任何工作区")
        target = self._targets.get(target_id)
        if target is None:
            raise LookupError(f"Gateway 工作区不存在: {target_id}")
        return target

    def upsert_remote_gateway(
        self,
        connection: RemoteGatewayConnection,
        *,
        runtime: WorkspaceRuntime | None = None,
    ) -> None:
        self._remote_gateway_connections[connection.connection_id] = connection
        if runtime is not None:
            previous = self._remote_gateway_runtimes.pop(connection.connection_id, None)
            if previous is not None:
                previous.close()
            self._remote_gateway_runtimes[connection.connection_id] = runtime
        self._save()

    def remote_gateway_connection(
        self,
        connection_id: str,
    ) -> RemoteGatewayConnection:
        connection = self._remote_gateway_connections.get(connection_id)
        if connection is None:
            raise LookupError(f"未知远程 Gateway 连接: {connection_id}")
        return connection

    def remote_gateway_url(self, connection_id: str) -> str:
        runtime = self._remote_gateway_runtimes.get(connection_id)
        if runtime is None:
            raise LookupError(f"远程 Gateway 隧道尚未连接: {connection_id}")
        return runtime.service_urls["workspace_api"]

    def remote_gateway_connections(self) -> tuple[RemoteGatewayConnection, ...]:
        return tuple(self._remote_gateway_connections.values())

    def _close_unused_remote_gateway(self, connection_id: str | None) -> None:
        if connection_id is None:
            return
        if any(
            target.remote_gateway_connection_id == connection_id
            for target in self._targets.values()
        ):
            return
        runtime = self._remote_gateway_runtimes.pop(connection_id, None)
        if runtime is not None:
            runtime.close()
        self._remote_gateway_connections.pop(connection_id, None)
        FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).remove(connection_id)

    def resolve_service_url(
        self,
        workspace_id: str,
        service: GatewayServiceName,
    ) -> str:
        target = self.resolve(workspace_id)
        if target.connection_kind == "remote_gateway":
            connection_id = target.remote_gateway_connection_id
            remote_workspace_id = target.remote_workspace_id
            if connection_id is None or remote_workspace_id is None:
                raise RuntimeError(f"远程投影工作区缺少所属 Gateway 信息: {workspace_id}")
            gateway_url = self.remote_gateway_url(connection_id)
            if service not in target.remote_service_names:
                raise LookupError(
                    f"远程工作区未提供服务: workspace_id={workspace_id}, service={service}"
                )
            if service == "workspace_api":
                return gateway_url
            service_path = (
                "terminal-manager"
                if service == "terminal_manager"
                else "browser-manager"
            )
            return (
                f"{gateway_url}/api/gateway/workspaces/"
                f"{remote_workspace_id}/{service_path}"
            )
        runtime = self._runtimes.get(target.workspace_id)
        if runtime is None:
            raise LookupError(f"工作区运行时尚未连接: {workspace_id}")
        service_url = runtime.service_urls.get(service)
        if service_url is None:
            raise LookupError(
                f"工作区未提供服务: workspace_id={workspace_id}, service={service}"
            )
        return service_url

    def targets(self) -> tuple[WorkspaceTarget, ...]:
        return tuple(self._targets.values())

    def has_target(self, workspace_id: str) -> bool:
        return workspace_id in self._targets

    def mark_connection_error(self, workspace_id: str, error: str) -> None:
        target = self.resolve(workspace_id)
        target.connection_error = error
        self._save()

    async def list_dtos(self) -> list[GatewayWorkspaceDTO]:
        targets = list(self._targets.values())
        async with httpx.AsyncClient(timeout=2) as client:
            async def build_dto(target: WorkspaceTarget) -> GatewayWorkspaceDTO:
                runtime = self._runtimes.get(target.workspace_id)
                runtime_service_urls = (
                    dict(runtime.service_urls) if runtime is not None else {}
                )
                if target.connection_kind == "remote_gateway":
                    for service in (
                        "workspace_api",
                        "terminal_manager",
                        "browser_manager",
                    ):
                        try:
                            runtime_service_urls[service] = self.resolve_service_url(
                                target.workspace_id,
                                service,
                            )
                        except LookupError:
                            continue
                status = "offline"
                workspace_service_status: GatewayServiceStatus = "offline"
                workspace_service_error: str | None = None
                try:
                    backend_url = self.resolve_service_url(
                        target.workspace_id,
                        "workspace_api",
                    )
                    response = await client.get(
                        f"{backend_url.rstrip('/')}/api/v1/health",
                        headers=self._target_headers(target),
                    )
                    if response.status_code == 200:
                        status = "ready"
                        workspace_service_status = "ready"
                    else:
                        workspace_service_error = (
                            f"健康检查返回 HTTP {response.status_code}"
                        )
                except Exception as error:
                    status = "offline"
                    workspace_service_error = str(error)
                config_reload = GatewayConfigReloadStatusDTO()
                if status == "ready":
                    try:
                        config_response = await client.get(
                            f"{backend_url.rstrip('/')}/api/v1/config/reload-status",
                            headers=self._target_headers(target),
                        )
                        if config_response.status_code != 200:
                            raise RuntimeError(
                                "配置状态接口返回 HTTP "
                                f"{config_response.status_code}"
                            )
                        config_payload = config_response.json()
                        config_data = config_payload.get("data")
                        if not isinstance(config_data, dict):
                            raise ValueError("配置状态接口缺少 data 对象")
                        config_reload = GatewayConfigReloadStatusDTO(
                            available=True,
                            healthy=config_data.get("healthy"),
                            revision=config_data.get("revision"),
                            restart_required=bool(
                                config_data.get("restart_required", False)
                            ),
                            reason=config_data.get("reason"),
                            changed_sections=list(
                                config_data.get("changed_sections", [])
                            ),
                            last_error=config_data.get("last_error"),
                        )
                    except Exception as error:
                        config_reload = GatewayConfigReloadStatusDTO(
                            available=False,
                            error=str(error),
                        )
                health_paths: dict[GatewayServiceName, str] = {
                    "workspace_api": "/api/v1/health",
                    "terminal_manager": "/health",
                    "browser_manager": "/health",
                }
                def service_dto(
                    service: GatewayServiceName,
                    service_status: GatewayServiceStatus,
                    *,
                    error: str | None = None,
                ) -> GatewayServiceStatusDTO:
                    local_url = (
                        runtime_service_urls.get(service)
                    )
                    parsed_url = urlparse(local_url) if local_url is not None else None
                    return GatewayServiceStatusDTO(
                        status=service_status,
                        health_path=health_paths[service],
                        local_url=local_url,
                        local_port=parsed_url.port if parsed_url is not None else None,
                        error=error,
                    )

                service_statuses: dict[str, GatewayServiceStatusDTO] = {
                    "workspace_api": service_dto(
                        "workspace_api",
                        workspace_service_status,
                        error=workspace_service_error,
                    )
                }
                for service, health_path in health_paths.items():
                    if service == "workspace_api":
                        continue
                    if service not in runtime_service_urls:
                        service_statuses[service] = service_dto(
                            service,
                            "unavailable",
                        )
                        continue
                    service_url = runtime_service_urls[service]
                    try:
                        response = await client.get(
                            f"{service_url.rstrip('/')}{health_path}",
                            headers=self._target_headers(target),
                        )
                        service_statuses[service] = service_dto(
                            service,
                            "ready" if response.status_code == 200 else "offline",
                            error=(
                                None
                                if response.status_code == 200
                                else f"健康检查返回 HTTP {response.status_code}"
                            ),
                        )
                    except Exception as error:
                        service_statuses[service] = service_dto(
                            service,
                            "offline",
                            error=str(error),
                        )
                remote_connection = (
                    self.remote_gateway_connection(
                        target.remote_gateway_connection_id
                    )
                    if target.remote_gateway_connection_id is not None
                    else None
                )
                return GatewayWorkspaceDTO(
                    workspace_id=target.workspace_id,
                    parent_workspace_id=target.parent_workspace_id,
                    name=target.name,
                    root_path=target.root_path,
                    backend_url=target.backend_url,
                    connection_kind=target.connection_kind,
                    status=status,
                    active=target.workspace_id == self._active_workspace_id,
                    managed=target.managed,
                    removable=target.removable,
                    system_default=target.system_default,
                    runtime_action=(
                        (
                            "reconnect_remote_gateway"
                            if target.connection_error
                            else (
                                "safe_restart_managed_backend"
                                if target.managed
                                else "probe_external_backend"
                            )
                        )
                        if target.connection_kind == "remote_gateway"
                        else (
                            "safe_restart_managed_backend"
                            if target.managed
                            else "probe_external_backend"
                        )
                    ),
                    config_reload=config_reload,
                    remote=(
                        GatewayRemoteConnectionSummaryDTO(
                            gateway_connection_id=remote_connection.connection_id,
                            remote_workspace_id=target.remote_workspace_id,
                            gateway_id=remote_connection.remote_gateway_id,
                            name=remote_connection.name,
                            host=remote_connection.host,
                            port=remote_connection.port,
                            username=remote_connection.username,
                            ssh_config_host=remote_connection.ssh_config_host,
                            remote_gateway_port=remote_connection.remote_gateway_port,
                        )
                        if target.connection_kind == "remote_gateway"
                        and remote_connection is not None
                        and target.remote_workspace_id is not None
                        else None
                    ),
                    connection_error=target.connection_error,
                    services=service_statuses,
                    checked_at=datetime.now(timezone.utc).isoformat(),
                )

            return list(await asyncio.gather(*(build_dto(target) for target in targets)))

    @staticmethod
    def _target_headers(target: WorkspaceTarget) -> dict[str, str]:
        if target.connection_kind != "remote_gateway":
            return {"X-Local-Token": "local-dev-token"}
        connection_id = target.remote_gateway_connection_id
        remote_workspace_id = target.remote_workspace_id
        if connection_id is None or remote_workspace_id is None:
            raise RuntimeError(
                f"远程投影工作区缺少连接信息: {target.workspace_id}"
            )
        credential = FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).get(connection_id)
        return {
            "X-BoxTeam-Federation-Token": credential.token,
            "X-BoxTeam-Workspace-Id": remote_workspace_id,
        }

    def _default_workspace_id(self) -> str | None:
        for target in self._targets.values():
            if target.system_default:
                return target.workspace_id
        return next(iter(self._targets), None)

    @staticmethod
    def _migrate_legacy_workspace_id(
        *,
        workspace_id: str,
        root_path: str,
        backend_url: str,
        connection_kind: GatewayConnectionKind,
        managed: bool,
        system_default: bool,
        schema_version: int,
    ) -> str:
        if (
            schema_version < 6
            and connection_kind == "local"
            and (managed or system_default)
        ):
            return build_managed_local_workspace_id(root_path)
        if not is_legacy_workspace_id(workspace_id):
            return workspace_id
        return build_workspace_id(
            connection_kind,
            root_path,
            backend_url,
        )

    def _load(self) -> None:
        if not self._storage_path.exists():
            return
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
        raw_remote_connections = payload.get("remote_gateway_connections", [])
        if not isinstance(raw_remote_connections, list):
            raise ValueError("Gateway registry remote_gateway_connections 必须是数组")
        for item in raw_remote_connections:
            if not isinstance(item, dict):
                raise ValueError("Gateway registry 远程 Gateway 连接必须是对象")
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
            )
            self._remote_gateway_connections[connection.connection_id] = connection
        targets = payload.get("targets", [])
        if not isinstance(targets, list):
            raise ValueError(f"Gateway registry targets 必须是数组: {self._storage_path}")
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
            target = WorkspaceTarget(
                workspace_id=workspace_id,
                name=str(item["name"]),
                root_path=root_path,
                backend_url=backend_url,
                connection_kind=connection_kind,
                # TODO: 所有 schema<4 的 Registry 完成一次性迁移后，在下一个持久化格式大版本移除默认值。
                name_customized=bool(item.get("name_customized", False)),
                managed=managed,
                removable=bool(item.get("removable", True)),
                system_default=system_default,
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
                remote_service_names=tuple(item.get("remote_service_names", ())),
                connection_error=(
                    str(item["connection_error"])
                    if item.get("connection_error") is not None
                    else None
                ),
            )
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
        active_id = payload.get("active_workspace_id")
        migrated_active_id = (
            workspace_id_remap.get(active_id, active_id)
            if isinstance(active_id, str)
            else None
        )
        if migrated_active_id is not None and migrated_active_id in self._targets:
            self._active_workspace_id = migrated_active_id
        elif self._targets:
            self._active_workspace_id = next(iter(self._targets))
        self._order_customized = bool(payload.get("order_customized", False))
        if migrated:
            self._save()

    def _validate_parent_graph(self) -> None:
        for workspace_id in self._targets:
            visited: set[str] = set()
            current_id: str | None = workspace_id
            while current_id is not None:
                if current_id in visited:
                    raise ValueError(
                        "Gateway registry 工作区父子关系形成循环: "
                        f"workspace_id={workspace_id}"
                    )
                visited.add(current_id)
                current_id = self._targets[current_id].parent_workspace_id

    def _save(self) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _REGISTRY_SCHEMA_VERSION,
            "active_workspace_id": self._active_workspace_id,
            "order_customized": self._order_customized,
            "remote_gateway_connections": [
                asdict(connection)
                for connection in self._remote_gateway_connections.values()
            ],
            "targets": [asdict(target) for target in self._targets.values()],
        }
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
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self._storage_path)
        finally:
            temporary_path.unlink(missing_ok=True)
