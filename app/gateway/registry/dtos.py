from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

import httpx

from app.core.mounted_workspaces import mounted_workspace_manifest_digest
from app.gateway.runtime.local_workspace import validate_workspace_health_identity
from app.gateway.service_types import GatewayServiceName
from app.schemas.gateway.config import GatewayConfigReloadStatusDTO
from app.schemas.gateway.workspaces import (
    GatewayRemoteConnectionSummaryDTO,
    GatewayServiceStatus,
    GatewayServiceStatusDTO,
    GatewayWorkspaceDTO,
)

from .core import WorkspaceTarget

if TYPE_CHECKING:
    from app.gateway.runtime.managed_backend import ManagedBackendContract

    from . import GatewayWorkspaceRegistry


class RegistryDtosMixin:
    async def list_dtos(self, *, check_health: bool = True) -> list[GatewayWorkspaceDTO]:
        active_workspace_id = self._active_workspace_id
        targets = list(self._targets.values())
        backend_owners = tuple((owner, owner.contract) for owner in self._backend_owners.values())
        backend_by_workspace_id = {
            workspace_id: (owner, contract)
            for owner, contract in backend_owners
            for workspace_id in contract.workspace_ids
        }
        backend_mount_digests: dict[str, str] = {}

        def expected_backend_mount_digest(
            contract: ManagedBackendContract,
        ) -> str:
            digest = backend_mount_digests.get(contract.owner_id)
            if digest is None:
                mounts = cast("GatewayWorkspaceRegistry", self).resolve_backend_mounts(
                    contract
                )
                digest = mounted_workspace_manifest_digest(mounts)
                backend_mount_digests[contract.owner_id] = digest
            return digest

        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            async def build_dto(target: WorkspaceTarget) -> GatewayWorkspaceDTO:
                backend_owner, backend_contract = backend_by_workspace_id.get(
                    target.workspace_id, (None, None)
                )
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
                if backend_owner is not None and not check_health:
                    try:
                        backend_owner.assert_healthy()
                    except RuntimeError as error:
                        status = "offline"
                        workspace_service_status = "offline"
                        workspace_service_error = str(error)
                if check_health:
                    try:
                        if backend_owner is not None:
                            backend_owner.assert_healthy()
                            # 控制面读取已公布合同，不经过业务代理的生命周期暂拒入口。
                            backend_url = backend_contract.backend_url
                        else:
                            backend_url = self.resolve_service_url(
                                target.workspace_id,
                                "workspace_api",
                            )
                        response = await client.get(
                            f"{backend_url.rstrip('/')}/api/v1/health",
                            headers=self._target_headers(target),
                        )
                        if response.status_code == 200:
                            health_payload = validate_workspace_health_identity(
                                response.json(), target.backend_workspace_id,
                            )
                            if backend_owner is not None and (
                                health_payload.get("backend_generation")
                                != backend_contract.generation
                                or health_payload.get("mount_manifest_digest")
                                != expected_backend_mount_digest(backend_contract)
                                or health_payload.get("status") != "ok"
                            ):
                                raise RuntimeError(
                                    "managed Backend 完整组健康证明不匹配"
                                )
                            status = "ready"
                            workspace_service_status = "ready"
                            workspace_service_error = None
                        else:
                            status = "offline"
                            workspace_service_status = "offline"
                            workspace_service_error = (
                                f"健康检查返回 HTTP {response.status_code}"
                            )
                    except Exception as error:  # noqa: BLE001 — 列表逐项收集真实故障并返回明确错误状态。
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
                            raise ValueError("配置状态接口缺少 data 对象")  # noqa: TRY004 — 保留配置健康协议既有拒绝类型。
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
                    except Exception as error:  # noqa: BLE001 — 列表逐项收集真实故障并返回明确错误状态。
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
                    except Exception as error:  # noqa: BLE001 — 列表逐项收集真实故障并返回明确错误状态。
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
                    backend_workspace_id=target.backend_workspace_id,
                    backend_generation=backend_contract.generation if backend_contract is not None else target.remote_backend_generation,
                    affected_workspace_ids=list(backend_contract.workspace_ids) if backend_contract is not None else list(target.remote_affected_workspace_ids) if target.remote_backend_generation is not None else [target.workspace_id],
                    parent_workspace_id=target.parent_workspace_id,
                    name=target.name,
                    backend_url=target.backend_url,
                    connection_kind=target.connection_kind,
                    status=status,
                    active=target.workspace_id == active_workspace_id,
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
                    checked_at=datetime.now(UTC).isoformat(),
                )

            items = list(await asyncio.gather(*(build_dto(target) for target in targets)))
            by_id = {item.workspace_id: item for item in items}
            for owner, contract in backend_owners:
                failed = [
                    by_id[workspace_id] for workspace_id in contract.workspace_ids
                    if by_id[workspace_id].status != "ready"
                    or any(service.status == "offline" for service in by_id[workspace_id].services.values())
                ]
                if failed:
                    error = owner.error or "; ".join(
                        f"{item.workspace_id}/{name}: {service.error}"
                        for item in failed for name, service in item.services.items() if service.status == "offline"
                    )
                    # 切换期间的探针失败只作为本次响应事实；旧快照也不能污染新合同。
                    if (
                        owner.contract is contract and not owner.lock.locked()
                        and owner.process is not None and owner.error != error
                    ):
                        self.mark_backend_error(owner, error)
                    for workspace_id in contract.workspace_ids:
                        item = by_id[workspace_id]
                        item.status = "offline"
                        item.connection_error = error
                        item.services["workspace_api"].status = "offline"
                        item.services["workspace_api"].error = error
            return items
