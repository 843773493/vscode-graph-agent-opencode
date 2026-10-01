from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.gateway.service_types import GatewayServiceName
from app.schemas.gateway import (
    GatewayConfigReloadStatusDTO,
    GatewayRemoteConnectionSummaryDTO,
    GatewayServiceStatus,
    GatewayServiceStatusDTO,
    GatewayWorkspaceDTO,
)

from .core import WorkspaceTarget


class RegistryDtosMixin:
    async def list_dtos(self, *, check_health: bool = True) -> list[GatewayWorkspaceDTO]:
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
                status = "ready" if "workspace_api" in runtime_service_urls else "offline"
                workspace_service_status: GatewayServiceStatus = (
                    "ready" if status == "ready" else "offline"
                )
                workspace_service_error: str | None = (
                    None
                    if status == "ready"
                    else target.connection_error
                    or f"工作区运行时尚未连接: {target.workspace_id}"
                )
                backend_url: str | None = None
                if check_health:
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
                            workspace_service_error = None
                        else:
                            status = "offline"
                            workspace_service_status = "offline"
                            workspace_service_error = (
                                f"健康检查返回 HTTP {response.status_code}"
                            )
                    except Exception as error:
                        status = "offline"
                        workspace_service_status = "offline"
                        workspace_service_error = str(error)
                config_reload = GatewayConfigReloadStatusDTO()
                if check_health and status == "ready":
                    if backend_url is None:
                        raise RuntimeError(
                            f"工作区健康检查已通过但缺少后端地址: {target.workspace_id}"
                        )
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
                    if not check_health:
                        service_statuses[service] = service_dto(service, "ready")
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
                            (
                                "safe_restart_managed_backend"
                                if runtime is not None
                                else "start_managed_backend"
                            )
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
                            config_event_cursor=(
                                remote_connection.remote_config_event_cursor
                            ),
                            config_reload_state=remote_connection.remote_config_state,
                            restart_required=remote_connection.remote_restart_required,
                            candidate_ref=remote_connection.remote_candidate_ref,
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
