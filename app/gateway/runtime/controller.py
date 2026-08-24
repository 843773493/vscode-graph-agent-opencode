from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Literal

import httpx

from app.core.path_utils import get_gateway_root
from app.gateway.auth import LOCAL_TOKEN
from app.gateway.credentials import FederationCredentialStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.remote_gateway import (
    reconnect_remote_gateway,
    refresh_remote_gateway_projections,
)
from app.gateway.runtime.local_workspace import (
    restart_managed_workspace_backend,
    start_managed_local_workspace_runtime,
)
from app.gateway.runtime.process import wait_for_http_ok
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.schemas.gateway import (
    GatewayRuntimeBlockerDTO,
    GatewayRuntimeRestartResultDTO,
    GatewayRuntimeStateResultDTO,
    GatewayWorkspaceListDTO,
)


class GatewayWorkspaceRuntimeController:
    def __init__(
        self,
        *,
        registry: GatewayWorkspaceRegistry,
        project_root: Path,
        log_dir: Path,
        on_registry_reconciled: Callable[[], Awaitable[None]] | None = None,
        drain_timeout_seconds: float = 30,
        drain_poll_interval_seconds: float = 0.25,
        health_request_timeout_seconds: float = 2,
        health_poll_interval_seconds: float = 0.5,
        connection_drain_timeout_seconds: float = 2,
        default_skill_groups: Sequence[str] = (),
    ) -> None:
        self._registry = registry
        self._project_root = project_root
        self._log_dir = log_dir
        self._on_registry_reconciled = on_registry_reconciled
        self._drain_timeout_seconds = drain_timeout_seconds
        self._drain_poll_interval_seconds = drain_poll_interval_seconds
        self._health_request_timeout_seconds = health_request_timeout_seconds
        self._health_poll_interval_seconds = health_poll_interval_seconds
        self._connection_drain_timeout_seconds = connection_drain_timeout_seconds
        self._default_skill_groups = tuple(default_skill_groups)
        self._locks: dict[str, asyncio.Lock] = {}

    async def start_managed_backend(
        self,
        workspace_id: str,
        *,
        request_id: str,
    ) -> GatewayRuntimeStateResultDTO:
        async with self._lock(workspace_id):
            target = self._registry.resolve(workspace_id)
            if target.connection_kind == "remote_gateway":
                return await self._delegated_runtime_state(
                    target,
                    "start",
                    request_id=request_id,
                )
            if target.connection_kind != "local" or not target.managed:
                raise ValueError("只有 Gateway 托管的本地工作区可以启动后端")
            if self._registry.has_runtime(workspace_id):
                raise ValueError(f"工作区后端已经启动: {target.name}")
            workspace_root = Path(target.root_path).expanduser().resolve()
            if not workspace_root.is_dir():
                raise FileNotFoundError(f"工作区目录不存在: {workspace_root}")
            runtime = await start_managed_local_workspace_runtime(
                project_root=self._project_root,
                workspace_root=workspace_root,
                log_dir=self._log_dir,
                reusable_service_urls=target.local_service_urls,
                health_request_timeout_seconds=self._health_request_timeout_seconds,
                health_poll_interval_seconds=self._health_poll_interval_seconds,
                connection_drain_timeout_seconds=(
                    self._connection_drain_timeout_seconds
                ),
                default_skill_groups=self._default_skill_groups,
            )
            target.backend_url = runtime.service_urls["workspace_api"]
            target.local_service_urls = {
                "browser_manager": runtime.service_urls["browser_manager"]
            }
            target.connection_error = None
            self._registry.upsert(target, runtime=runtime, activate=False)
            return await self._state_result(workspace_id, "started")

    async def stop_managed_backend(
        self,
        workspace_id: str,
        *,
        request_id: str,
    ) -> GatewayRuntimeStateResultDTO:
        async with self._lock(workspace_id):
            target = self._registry.resolve(workspace_id)
            if target.connection_kind == "remote_gateway":
                return await self._delegated_runtime_state(
                    target,
                    "stop",
                    request_id=request_id,
                )
            target, _ = self._managed_local_target(workspace_id)
            if target.system_default or not target.removable:
                raise PermissionError(f"默认工作区不能关闭: {target.name}")
            await self._runtime_action(
                target.backend_url,
                "/api/v1/runtime/drain",
                request_id=request_id,
            )
            deadline = asyncio.get_running_loop().time() + self._drain_timeout_seconds
            blockers: list[GatewayRuntimeBlockerDTO] = []
            while True:
                runtime_status = await self._runtime_status(
                    target.backend_url,
                    request_id=request_id,
                )
                blockers = self._parse_blockers(runtime_status.get("blockers"))
                if not blockers:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    await self._runtime_action(
                        target.backend_url,
                        "/api/v1/runtime/drain/cancel",
                        request_id=request_id,
                    )
                    return await self._state_result(
                        workspace_id,
                        "blocked",
                        blockers=blockers,
                    )
                await asyncio.sleep(self._drain_poll_interval_seconds)
            self._registry.stop_managed_runtime(workspace_id)
            return await self._state_result(workspace_id, "stopped")

    async def safe_restart_managed_backend(
        self,
        workspace_id: str,
        *,
        request_id: str,
    ) -> GatewayRuntimeRestartResultDTO:
        async with self._lock(workspace_id):
            target = self._registry.resolve(workspace_id)
            if target.connection_kind == "remote_gateway":
                return await self._delegated_restart(
                    target,
                    request_id=request_id,
                    forced=False,
                )
            target, runtime = self._managed_local_target(workspace_id)
            await self._runtime_action(
                target.backend_url,
                "/api/v1/runtime/drain",
                request_id=request_id,
            )
            deadline = (
                asyncio.get_running_loop().time() + self._drain_timeout_seconds
            )
            blockers: list[GatewayRuntimeBlockerDTO] = []
            while True:
                runtime_status = await self._runtime_status(
                    target.backend_url,
                    request_id=request_id,
                )
                blockers = self._parse_blockers(runtime_status.get("blockers"))
                if not blockers:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    await self._runtime_action(
                        target.backend_url,
                        "/api/v1/runtime/drain/cancel",
                        request_id=request_id,
                    )
                    return await self._restart_result(
                        workspace_id=workspace_id,
                        status="blocked",
                        forced=False,
                        blockers=blockers,
                    )
                await asyncio.sleep(self._drain_poll_interval_seconds)

            await self._restart_backend(target, runtime)
            return await self._restart_result(
                workspace_id=workspace_id,
                status="restarted",
                forced=False,
            )

    async def force_restart_managed_backend(
        self,
        workspace_id: str,
        *,
        request_id: str,
    ) -> GatewayRuntimeRestartResultDTO:
        async with self._lock(workspace_id):
            target = self._registry.resolve(workspace_id)
            if target.connection_kind == "remote_gateway":
                return await self._delegated_restart(
                    target,
                    request_id=request_id,
                    forced=True,
                )
            target, runtime = self._managed_local_target(workspace_id)
            await self._runtime_action(
                target.backend_url,
                "/api/v1/runtime/drain",
                request_id=request_id,
            )
            runtime_status = await self._runtime_status(
                target.backend_url,
                request_id=request_id,
            )
            blockers = self._parse_blockers(runtime_status.get("blockers"))
            await self._runtime_action(
                target.backend_url,
                "/api/v1/runtime/drain/force",
                request_id=request_id,
            )
            await self._restart_backend(target, runtime)
            return await self._restart_result(
                workspace_id=workspace_id,
                status="restarted",
                forced=True,
                blockers=blockers,
            )

    def _managed_local_target(
        self,
        workspace_id: str,
    ) -> tuple[WorkspaceTarget, WorkspaceRuntime]:
        target = self._registry.resolve(workspace_id)
        if target.connection_kind != "local" or not target.managed:
            raise ValueError("只有 Gateway 托管的本地工作区可以重启后端")
        return target, self._registry.managed_runtime(workspace_id)

    async def _restart_backend(
        self,
        target: WorkspaceTarget,
        runtime: WorkspaceRuntime,
    ) -> None:
        await restart_managed_workspace_backend(
            runtime=runtime,
            project_root=self._project_root,
            workspace_root=Path(target.root_path),
            log_dir=self._log_dir,
            health_request_timeout_seconds=self._health_request_timeout_seconds,
            health_poll_interval_seconds=self._health_poll_interval_seconds,
            connection_drain_timeout_seconds=self._connection_drain_timeout_seconds,
            default_skill_groups=self._default_skill_groups,
        )
        self._registry.invalidate_route(target.workspace_id)
        target.connection_error = None
        self._registry.upsert(target, activate=False)

    async def _runtime_status(
        self,
        backend_url: str,
        *,
        request_id: str,
    ) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{backend_url.rstrip('/')}/api/v1/runtime/status",
                headers=self._backend_headers(request_id),
            )
            response.raise_for_status()
        return self._response_data(response)

    async def _runtime_action(
        self,
        backend_url: str,
        path: str,
        *,
        request_id: str,
    ) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{backend_url.rstrip('/')}{path}",
                headers=self._backend_headers(request_id),
            )
            response.raise_for_status()
        return self._response_data(response)

    @staticmethod
    def _backend_headers(request_id: str) -> dict[str, str]:
        return {
            "X-Local-Token": LOCAL_TOKEN,
            "X-Request-ID": request_id,
        }

    @staticmethod
    def _response_data(response: httpx.Response) -> dict[str, object]:
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Workspace API 生命周期响应缺少 data: {response.text[:300]}"
            )
        return data

    @staticmethod
    def _parse_blockers(value: object) -> list[GatewayRuntimeBlockerDTO]:
        if not isinstance(value, list):
            raise RuntimeError("Workspace API 生命周期响应缺少 blockers 数组")
        return [GatewayRuntimeBlockerDTO.model_validate(item) for item in value]

    async def _restart_result(
        self,
        *,
        workspace_id: str,
        status: Literal["restarted", "blocked"],
        forced: bool,
        blockers: list[GatewayRuntimeBlockerDTO] | None = None,
    ) -> GatewayRuntimeRestartResultDTO:
        return GatewayRuntimeRestartResultDTO(
            workspace_id=workspace_id,
            status=status,
            forced=forced,
            blockers=blockers or [],
            workspaces=GatewayWorkspaceListDTO(
                active_workspace_id=self._registry.active_workspace_id,
                items=await self._registry.list_dtos(),
            ),
        )

    async def _state_result(
        self,
        workspace_id: str,
        status: Literal["started", "stopped", "blocked"],
        *,
        blockers: list[GatewayRuntimeBlockerDTO] | None = None,
    ) -> GatewayRuntimeStateResultDTO:
        return GatewayRuntimeStateResultDTO(
            workspace_id=workspace_id,
            status=status,
            blockers=blockers or [],
            workspaces=GatewayWorkspaceListDTO(
                active_workspace_id=self._registry.active_workspace_id,
                items=await self._registry.list_dtos(),
            ),
        )

    async def reconnect_ssh(self, workspace_id: str) -> None:
        async with self._lock(workspace_id):
            target = self._registry.resolve(workspace_id)
            if (
                target.connection_kind != "remote_gateway"
                or target.remote_gateway_connection_id is None
            ):
                raise ValueError("只有远程 Gateway 工作区可以重新连接 SSH 隧道")
            await reconnect_remote_gateway(
                registry=self._registry,
                connection_id=target.remote_gateway_connection_id,
                log_dir=self._log_dir,
                health_request_timeout_seconds=self._health_request_timeout_seconds,
                health_poll_interval_seconds=self._health_poll_interval_seconds,
            )
            await self._reconcile_registry_dependents()

    async def probe_external_backend(self, workspace_id: str) -> None:
        async with self._lock(workspace_id):
            target = self._registry.resolve(workspace_id)
            if target.connection_kind == "remote_gateway":
                await self._delegated_probe(target)
                return
            if target.connection_kind != "local" or target.managed:
                raise ValueError(
                    "只有外部本地后端可以执行重新探测"
                )
            await wait_for_http_ok(
                f"{target.backend_url.rstrip('/')}/api/v1/health",
                request_timeout_seconds=self._health_request_timeout_seconds,
                poll_interval_seconds=self._health_poll_interval_seconds,
            )
            target.connection_error = None
            self._registry.upsert(target, activate=False)

    def _lock(self, workspace_id: str) -> asyncio.Lock:
        lock = self._locks.get(workspace_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[workspace_id] = lock
        return lock

    async def _delegated_probe(self, target: WorkspaceTarget) -> None:
        connection_id = target.remote_gateway_connection_id
        remote_workspace_id = target.remote_workspace_id
        if connection_id is None or remote_workspace_id is None:
            raise RuntimeError("远程投影工作区缺少所属 Gateway 信息")
        credential = FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).get(connection_id)
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                (
                    f"{self._registry.remote_gateway_url(connection_id)}"
                    f"/api/gateway/workspaces/{remote_workspace_id}/probe"
                ),
                headers={
                    "X-BoxTeam-Federation-Token": credential.token,
                },
            )
            response.raise_for_status()
        target.connection_error = None
        self._registry.upsert(target, activate=False)

    async def _delegated_restart(
        self,
        target: WorkspaceTarget,
        *,
        request_id: str,
        forced: bool,
    ) -> GatewayRuntimeRestartResultDTO:
        if not target.managed:
            raise ValueError("远程 Gateway 报告该后端不是托管目标，只能重新探测")
        connection_id = target.remote_gateway_connection_id
        remote_workspace_id = target.remote_workspace_id
        if connection_id is None or remote_workspace_id is None:
            raise RuntimeError("远程投影工作区缺少所属 Gateway 信息")
        gateway_url = self._registry.remote_gateway_url(connection_id)
        credential = FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).get(connection_id)
        path = "restart-force" if forced else "restart-safe"
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(
                (
                    f"{gateway_url}/api/gateway/workspaces/"
                    f"{remote_workspace_id}/runtime/{path}"
                ),
                headers={
                    "X-BoxTeam-Federation-Token": credential.token,
                    "X-Request-ID": request_id,
                },
            )
            response.raise_for_status()
        data = self._response_data(response)
        blockers = self._parse_blockers(data.get("blockers"))
        status = data.get("status")
        if status not in {"restarted", "blocked"}:
            raise RuntimeError(f"远程 Gateway 返回未知重启状态: {status}")
        return await self._restart_result(
            workspace_id=target.workspace_id,
            status=status,
            forced=forced,
            blockers=blockers,
        )

    async def _delegated_runtime_state(
        self,
        target: WorkspaceTarget,
        action: Literal["start", "stop"],
        *,
        request_id: str,
    ) -> GatewayRuntimeStateResultDTO:
        if not target.managed:
            raise ValueError("远程 Gateway 报告该工作区不是托管目标")
        connection_id = target.remote_gateway_connection_id
        remote_workspace_id = target.remote_workspace_id
        if connection_id is None or remote_workspace_id is None:
            raise RuntimeError("远程投影工作区缺少所属 Gateway 信息")
        credential = FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).get(connection_id)
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                (
                    f"{self._registry.remote_gateway_url(connection_id)}"
                    f"/api/gateway/workspaces/{remote_workspace_id}/runtime/{action}"
                ),
                headers={
                    "X-BoxTeam-Federation-Token": credential.token,
                    "X-Request-ID": request_id,
                },
            )
            response.raise_for_status()
        data = self._response_data(response)
        status = data.get("status")
        if status not in {"started", "stopped", "blocked"}:
            raise RuntimeError(
                f"远程 Gateway 返回未知工作区状态: {status}"
            )
        blockers = self._parse_blockers(data.get("blockers"))
        await refresh_remote_gateway_projections(
            registry=self._registry,
            connection_id=connection_id,
        )
        await self._reconcile_registry_dependents()
        return await self._state_result(
            target.workspace_id,
            status,
            blockers=blockers,
        )

    async def _reconcile_registry_dependents(self) -> None:
        if self._on_registry_reconciled is not None:
            await self._on_registry_reconciled()


async def reconnect_gateway_workspace(
    *,
    registry: GatewayWorkspaceRegistry,
    workspace_id: str,
    project_root: Path,
    log_dir: Path,
    health_request_timeout_seconds: float = 2,
    health_poll_interval_seconds: float = 0.5,
    connection_drain_timeout_seconds: float = 2,
    default_skill_groups: Sequence[str] = (),
) -> None:
    """按注册信息重建工作区运行时，不改变稳定的 workspace_id。"""
    target = registry.resolve(workspace_id)

    if target.connection_kind == "remote_gateway":
        connection_id = target.remote_gateway_connection_id
        if connection_id is None:
            raise RuntimeError(f"远程工作区缺少 Gateway 连接信息: {workspace_id}")
        await reconnect_remote_gateway(
            registry=registry,
            connection_id=connection_id,
            log_dir=log_dir,
            health_request_timeout_seconds=health_request_timeout_seconds,
            health_poll_interval_seconds=health_poll_interval_seconds,
        )
        return

    if target.managed:
        runtime = await start_managed_local_workspace_runtime(
            project_root=project_root,
            workspace_root=Path(target.root_path),
            log_dir=log_dir,
            reusable_service_urls=target.local_service_urls,
            health_request_timeout_seconds=health_request_timeout_seconds,
            health_poll_interval_seconds=health_poll_interval_seconds,
            connection_drain_timeout_seconds=connection_drain_timeout_seconds,
            default_skill_groups=default_skill_groups,
        )
        target.backend_url = runtime.service_urls["workspace_api"]
        target.local_service_urls = {
            "browser_manager": runtime.service_urls["browser_manager"]
        }
    else:
        await wait_for_http_ok(
            f"{target.backend_url.rstrip('/')}/api/v1/health",
            request_timeout_seconds=health_request_timeout_seconds,
            poll_interval_seconds=health_poll_interval_seconds,
        )
        runtime = WorkspaceRuntime(service_urls={"workspace_api": target.backend_url})

    target.connection_error = None
    registry.upsert(target, runtime=runtime, activate=False)
