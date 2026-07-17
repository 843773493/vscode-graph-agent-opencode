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

from app.gateway.schemas import (
    GatewayConnectionKind,
    GatewayServiceStatus,
    GatewayServiceStatusDTO,
    GatewayWorkspaceDTO,
)
from app.gateway.service_runtime import WorkspaceRuntime
from app.gateway.service_types import GatewayServiceName, RemoteServiceSpec
from app.gateway.workspace_ids import (
    build_ssh_workspace_id,
    build_workspace_id,
    is_legacy_workspace_id,
)

_REGISTRY_SCHEMA_VERSION = 4


def _remote_services_from_json(value: object) -> tuple[RemoteServiceSpec, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("Gateway registry remote_services 必须是数组")
    services: list[RemoteServiceSpec] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Gateway registry remote_services 元素必须是对象")
        name = item.get("name")
        if name not in {"workspace_api", "terminal_manager", "browser_manager"}:
            raise ValueError(f"Gateway registry remote service name 非法: {name}")
        services.append(
            RemoteServiceSpec(
                name=name,
                host=str(item["host"]),
                port=int(item["port"]),
                required=bool(item.get("required", False)),
            )
        )
    return tuple(services)


@dataclass(frozen=True, slots=True)
class SshWorkspaceConnection:
    host: str
    port: int
    username: str
    private_key_path: str | None
    remote_backend_host: str
    remote_backend_port: int
    ssh_config_host: str | None = None
    remote_services: tuple[RemoteServiceSpec, ...] = ()


@dataclass(slots=True)
class WorkspaceTarget:
    workspace_id: str
    name: str
    root_path: str
    backend_url: str
    connection_kind: GatewayConnectionKind
    name_customized: bool = False
    managed: bool = False
    removable: bool = True
    system_default: bool = False
    ssh_connection: SshWorkspaceConnection | None = None
    connection_error: str | None = None


class GatewayWorkspaceRegistry:
    def __init__(self, *, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._targets: dict[str, WorkspaceTarget] = {}
        self._active_workspace_id: str | None = None
        self._order_customized = False
        self._runtimes: dict[str, WorkspaceRuntime] = {}
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

    def remove(self, workspace_id: str) -> None:
        target = self._targets.get(workspace_id)
        if target is None:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        if not target.removable or target.system_default:
            raise PermissionError(f"默认工作区不能删除: {target.name}")
        runtime = self._runtimes.pop(workspace_id, None)
        if runtime is not None:
            runtime.close()
        del self._targets[workspace_id]
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
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Gateway 工作区名称不能为空")
        target = self._targets.get(workspace_id)
        if target is None:
            raise KeyError(f"未知 Gateway 工作区: {workspace_id}")
        target.name = normalized_name
        target.name_customized = True
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

    def resolve_ssh_connection(self, workspace_id: str) -> SshWorkspaceConnection:
        target = self.resolve(workspace_id)
        if target.connection_kind != "ssh" or target.ssh_connection is None:
            raise ValueError(f"工作区不是可复用的 SSH 连接: {workspace_id}")
        return target.ssh_connection

    def resolve_service_url(
        self,
        workspace_id: str,
        service: GatewayServiceName,
    ) -> str:
        target = self.resolve(workspace_id)
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
                        headers={"X-Local-Token": "local-dev-token"},
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
                health_paths: dict[GatewayServiceName, str] = {
                    "workspace_api": "/api/v1/health",
                    "terminal_manager": "/health",
                    "browser_manager": "/health",
                }
                remote_services = {
                    service.name: service
                    for service in (
                        target.ssh_connection.remote_services
                        if target.ssh_connection is not None
                        else ()
                    )
                }

                def service_dto(
                    service: GatewayServiceName,
                    service_status: GatewayServiceStatus,
                    *,
                    error: str | None = None,
                ) -> GatewayServiceStatusDTO:
                    local_url = (
                        runtime.service_urls.get(service)
                        if runtime is not None
                        else None
                    )
                    parsed_url = urlparse(local_url) if local_url is not None else None
                    remote_service = remote_services.get(service)
                    return GatewayServiceStatusDTO(
                        status=service_status,
                        health_path=health_paths[service],
                        local_url=local_url,
                        local_port=parsed_url.port if parsed_url is not None else None,
                        remote_host=(
                            remote_service.host if remote_service is not None else None
                        ),
                        remote_port=(
                            remote_service.port if remote_service is not None else None
                        ),
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
                    if runtime is None or service not in runtime.service_urls:
                        service_statuses[service] = service_dto(
                            service,
                            "unavailable",
                        )
                        continue
                    service_url = runtime.service_urls[service]
                    try:
                        response = await client.get(
                            f"{service_url.rstrip('/')}{health_path}",
                            headers={"X-Local-Token": "local-dev-token"},
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
                return GatewayWorkspaceDTO(
                    workspace_id=target.workspace_id,
                    name=target.name,
                    root_path=target.root_path,
                    backend_url=target.backend_url,
                    connection_kind=target.connection_kind,
                    status=status,
                    active=target.workspace_id == self._active_workspace_id,
                    managed=target.managed,
                    removable=target.removable,
                    system_default=target.system_default,
                    remote=(
                        {
                            "host": target.ssh_connection.host,
                            "port": target.ssh_connection.port,
                            "username": target.ssh_connection.username,
                            "remote_backend_host": target.ssh_connection.remote_backend_host,
                            "remote_backend_port": target.ssh_connection.remote_backend_port,
                        }
                        if target.ssh_connection is not None
                        else {}
                    ),
                    connection_error=target.connection_error,
                    services=service_statuses,
                    checked_at=datetime.now(timezone.utc).isoformat(),
                )

            return list(await asyncio.gather(*(build_dto(target) for target in targets)))

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
        ssh_connection: SshWorkspaceConnection | None,
    ) -> str:
        if not is_legacy_workspace_id(workspace_id):
            return workspace_id
        if connection_kind == "ssh" and ssh_connection is not None:
            return build_ssh_workspace_id(
                root_path=root_path,
                host=ssh_connection.host,
                port=ssh_connection.port,
                username=ssh_connection.username,
                remote_backend_host=ssh_connection.remote_backend_host,
                remote_backend_port=ssh_connection.remote_backend_port,
            )
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
        targets = payload.get("targets", [])
        if not isinstance(targets, list):
            raise ValueError(f"Gateway registry targets 必须是数组: {self._storage_path}")
        workspace_id_remap: dict[str, str] = {}
        migrated = schema_version < _REGISTRY_SCHEMA_VERSION
        for item in targets:
            if not isinstance(item, dict):
                raise ValueError(f"Gateway registry target 必须是对象: {self._storage_path}")
            raw_ssh_connection = item.get("ssh_connection")
            ssh_connection = None
            if raw_ssh_connection is not None:
                if not isinstance(raw_ssh_connection, dict):
                    raise ValueError(
                        f"Gateway registry ssh_connection 必须是对象: {self._storage_path}"
                    )
                ssh_connection = SshWorkspaceConnection(
                    host=str(raw_ssh_connection["host"]),
                    port=int(raw_ssh_connection["port"]),
                    username=str(raw_ssh_connection["username"]),
                    private_key_path=(
                        str(raw_ssh_connection["private_key_path"])
                        if raw_ssh_connection.get("private_key_path") is not None
                        else None
                    ),
                    remote_backend_host=str(raw_ssh_connection["remote_backend_host"]),
                    remote_backend_port=int(raw_ssh_connection["remote_backend_port"]),
                    ssh_config_host=(
                        str(raw_ssh_connection["ssh_config_host"])
                        if raw_ssh_connection.get("ssh_config_host") is not None
                        else None
                    ),
                    remote_services=_remote_services_from_json(
                        raw_ssh_connection.get("remote_services")
                    ),
                )
            original_workspace_id = str(item["workspace_id"])
            root_path = str(item["root_path"])
            backend_url = str(item["backend_url"])
            connection_kind = item["connection_kind"]
            if connection_kind not in {"local", "ssh"}:
                raise ValueError(
                    "Gateway registry connection_kind 非法: "
                    f"workspace_id={original_workspace_id}, kind={connection_kind}"
                )
            # TODO: 旧 12 位 Gateway ID 完成一次性迁移后，在下一个持久化格式大版本移除。
            workspace_id = self._migrate_legacy_workspace_id(
                workspace_id=original_workspace_id,
                root_path=root_path,
                backend_url=backend_url,
                connection_kind=connection_kind,
                ssh_connection=ssh_connection,
            )
            if workspace_id in self._targets:
                raise ValueError(
                    "Gateway registry 工作区 ID 迁移后发生冲突: "
                    f"original={original_workspace_id}, migrated={workspace_id}"
                )
            workspace_id_remap[original_workspace_id] = workspace_id
            migrated = migrated or workspace_id != original_workspace_id
            target = WorkspaceTarget(
                workspace_id=workspace_id,
                name=str(item["name"]),
                root_path=root_path,
                backend_url=backend_url,
                connection_kind=connection_kind,
                # TODO: 所有 schema<4 的 Registry 完成一次性迁移后，在下一个持久化格式大版本移除默认值。
                name_customized=bool(item.get("name_customized", False)),
                managed=bool(item.get("managed", False)),
                removable=bool(item.get("removable", True)),
                system_default=bool(item.get("system_default", False)),
                ssh_connection=ssh_connection,
                connection_error=(
                    str(item["connection_error"])
                    if item.get("connection_error") is not None
                    else None
                ),
            )
            self._targets[target.workspace_id] = target
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

    def _save(self) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _REGISTRY_SCHEMA_VERSION,
            "active_workspace_id": self._active_workspace_id,
            "order_customized": self._order_customized,
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
