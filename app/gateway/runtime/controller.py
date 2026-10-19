from __future__ import annotations

import asyncio
import zlib
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx

from app.core.path_utils import get_gateway_root
from app.gateway.auth import LOCAL_TOKEN
from app.gateway.credentials import FederationCredentialStore
from app.gateway.proxy_upstream import load_proxy_gateway_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.remote_gateway import (
    reconnect_remote_gateway,
    refresh_remote_gateway_projections,
)
from app.gateway.runtime.consumer_protocol import GatewayRuntimeHealthProof
from app.gateway.runtime.group_actions import ManagedBackendGroupActions
from app.gateway.runtime.local_workspace import (
    wait_for_workspace_health,
)
from app.gateway.runtime.process import resolve_backend_workspace_id
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.schemas.gateway.lifecycle import (
    GatewayRuntimeBlockerDTO,
    GatewayRuntimeGroupRequest,
    GatewayRuntimeRestartResultDTO,
    GatewayRuntimeStateResultDTO,
)
from app.schemas.gateway.workspaces import GatewayWorkspaceListDTO

_LOCK_SHARDS = 64


class GatewayWorkspaceRuntimeController(ManagedBackendGroupActions):
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
        # 固定分片锁池：workspace_id 来自路由路径参数，长驻单例若按 id 建锁会随
        # 历史/探针 id 无界增长。同族缺陷（eade5389/bdd6ecd1）统一用固定分片池，
        # crc32 而非 hash() 以保证跨进程确定；同 id 恒得同一把锁，互斥语义不变。
        self._locks: tuple[asyncio.Lock, ...] = tuple(
            asyncio.Lock() for _ in range(_LOCK_SHARDS)
        )

    async def start_managed_backend(
        self, workspace_id: str, *, request_id: str,
        group_request: GatewayRuntimeGroupRequest | None = None,
    ) -> GatewayRuntimeStateResultDTO:
        return await self._managed_group_action(workspace_id, "start", request_id=request_id, group_request=group_request)

    async def stop_managed_backend(
        self, workspace_id: str, *, request_id: str,
        group_request: GatewayRuntimeGroupRequest | None = None,
    ) -> GatewayRuntimeStateResultDTO:
        return await self._managed_group_action(workspace_id, "stop", request_id=request_id, group_request=group_request)

    async def safe_restart_managed_backend(
        self, workspace_id: str, *, request_id: str,
        group_request: GatewayRuntimeGroupRequest | None = None,
    ) -> GatewayRuntimeRestartResultDTO:
        return await self._managed_group_action(workspace_id, "restart-safe", request_id=request_id, group_request=group_request)

    async def force_restart_managed_backend(
        self, workspace_id: str, *, request_id: str,
        group_request: GatewayRuntimeGroupRequest | None = None,
    ) -> GatewayRuntimeRestartResultDTO:
        return await self._managed_group_action(workspace_id, "restart-force", request_id=request_id, group_request=group_request)

    async def remove_managed_workspace(
        self, workspace_id: str, *, request_id: str,
        group_request: GatewayRuntimeGroupRequest | None = None, force: bool = False,
    ) -> GatewayRuntimeRestartResultDTO:
        return await self._managed_group_action(
            workspace_id, "restart-force" if force else "restart-safe",
            request_id=request_id, group_request=group_request, remove_target=True,
        )

    def update_health_controller_config(
        self,
        *,
        request_timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> None:
        """切换下一轮 Gateway 健康探测使用的 controller 参数。"""

        self.prepare_health_controller_config(
            request_timeout_seconds=request_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        self._health_request_timeout_seconds = request_timeout_seconds
        self._health_poll_interval_seconds = poll_interval_seconds

    def prepare_health_controller_config(
        self,
        *,
        request_timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> None:
        """只校验 health controller 参数，不改变当前探测窗口。"""

        if request_timeout_seconds <= 0:
            raise ValueError("Gateway 健康探测 request_timeout_seconds 必须大于 0")
        if poll_interval_seconds <= 0:
            raise ValueError("Gateway 健康探测 poll_interval_seconds 必须大于 0")

    def runtime_health_proof(
        self,
        *,
        generation: str,
        fencing_token_digest: str | None = None,
    ) -> GatewayRuntimeHealthProof:
        self.assert_health_controller_healthy()
        return GatewayRuntimeHealthProof(
            consumer_id="health-controller",
            generation=generation,
            state="healthy",
            details={
                "request_timeout_seconds": self._health_request_timeout_seconds,
                "poll_interval_seconds": self._health_poll_interval_seconds,
            },
            fencing_token_digest=fencing_token_digest,
        )

    def assert_health_controller_healthy(self) -> None:
        """确认健康控制器当前参数仍满足探测边界。"""

        if self._health_request_timeout_seconds <= 0:
            raise RuntimeError("Gateway 健康控制器 request_timeout_seconds 无效")
        if self._health_poll_interval_seconds <= 0:
            raise RuntimeError("Gateway 健康控制器 poll_interval_seconds 无效")

    async def _record_pending_restart_failure(
        self,
        target: WorkspaceTarget,
        *,
        candidate_ref: str,
        error: str,
        old_runtime_recovered: bool,
        request_id: str,
    ) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{target.backend_url.rstrip('/')}/api/v1/config/pending/restart-failed",
                params={"candidate_ref": candidate_ref},
                json={
                    "error": error,
                    "old_runtime_recovered": old_runtime_recovered,
                },
                headers=self._backend_headers(
                    request_id,
                    target.backend_workspace_id,
                ),
            )
            response.raise_for_status()

    async def _resolve_pending_restart(
        self,
        target: WorkspaceTarget,
        *,
        candidate_ref: str,
        request_id: str,
    ) -> None:
        """用候选后端的健康证明提交已成功启动的 Workspace 配置。"""

        async with httpx.AsyncClient(timeout=10) as client:
            health_response = await client.get(
                f"{target.backend_url.rstrip('/')}/api/v1/health",
                headers=self._backend_headers(
                    request_id,
                    target.backend_workspace_id,
                ),
            )
            health_response.raise_for_status()
            health_payload = health_response.json()
            if not isinstance(health_payload, dict):
                raise TypeError("Workspace 健康响应必须是 JSON 对象")
            if health_payload.get("workspace_id") != target.backend_workspace_id:
                raise RuntimeError(
                    "Workspace 健康响应 backend UUID 不匹配: "
                    f"expected={target.backend_workspace_id}, "
                    f"actual={health_payload.get('workspace_id')!r}"
                )
            health_proof = health_payload.get("config_proof")
            if not isinstance(health_proof, dict):
                raise TypeError("Workspace 健康响应缺少 config_proof")
            resolve_response = await client.post(
                f"{target.backend_url.rstrip('/')}/api/v1/config/pending/resolve",
                params={"candidate_ref": candidate_ref},
                json=health_proof,
                headers=self._backend_headers(
                    request_id,
                    target.backend_workspace_id,
                ),
            )
            resolve_response.raise_for_status()

    async def _runtime_status(
        self,
        target: WorkspaceTarget,
        *,
        request_id: str,
    ) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{target.backend_url.rstrip('/')}/api/v1/runtime/status",
                headers=self._backend_headers(
                    request_id,
                    target.backend_workspace_id,
                ),
            )
            response.raise_for_status()
        return self._response_data(response)

    async def _pending_startup_contract(
        self,
        target: WorkspaceTarget,
        *,
        request_id: str,
    ) -> dict[str, object] | None:
        async with httpx.AsyncClient(timeout=10) as client:
            status_response = await client.get(
                f"{target.backend_url.rstrip('/')}/api/v1/config/reload-status",
                headers=self._backend_headers(
                    request_id,
                    target.backend_workspace_id,
                ),
            )
        status_response.raise_for_status()
        status = self._response_data(status_response)
        if status.get("state") == "recovery_required":
            candidate_ref = status.get("candidate_ref")
            if not isinstance(candidate_ref, str) or not candidate_ref:
                raise RuntimeError(
                    "Workspace 报告 recovery_required，但缺少 candidate_ref"
                )
            async with httpx.AsyncClient(timeout=10) as client:
                retry_response = await client.post(
                    f"{target.backend_url.rstrip('/')}/api/v1/config/pending/retry",
                    params={"candidate_ref": candidate_ref},
                    headers=self._backend_headers(
                        request_id,
                        target.backend_workspace_id,
                    ),
                )
                retry_response.raise_for_status()
            status = self._response_data(retry_response)
        if status.get("restart_required") is not True:
            return None
        candidate_ref = status.get("candidate_ref")
        if not isinstance(candidate_ref, str) or not candidate_ref:
            raise RuntimeError(
                "Workspace 报告 restart_required，但缺少 candidate_ref"
            )
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{target.backend_url.rstrip('/')}/api/v1/config/pending/startup-contract",
                params={"candidate_ref": candidate_ref},
                headers=self._backend_headers(
                    request_id,
                    target.backend_workspace_id,
                ),
            )
            response.raise_for_status()
        contract = self._response_data(response)
        for field in (
            "candidate_ref",
            "candidate_id",
            "pending_revision",
            "candidate_digest",
            "effective_digest",
            "target_generation",
            "fencing_token",
            "secret_binding_digest",
        ):
            if field not in contract:
                raise TypeError(f"Workspace 启动契约缺少字段: {field}")
        if contract["candidate_ref"] != candidate_ref:
            raise RuntimeError("Workspace 启动契约 candidate_ref 回显不匹配")
        return contract

    async def _runtime_action(
        self,
        target: WorkspaceTarget,
        path: str,
        *,
        request_id: str,
    ) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{target.backend_url.rstrip('/')}{path}",
                headers=self._backend_headers(
                    request_id,
                    target.backend_workspace_id,
                ),
            )
            response.raise_for_status()
        return self._response_data(response)

    @staticmethod
    def _backend_headers(
        request_id: str,
        backend_workspace_id: str | None,
    ) -> dict[str, str]:
        if backend_workspace_id is None:
            raise RuntimeError("Workspace API 调用缺少 backend UUID")
        return {
            "X-Local-Token": LOCAL_TOKEN,
            "X-Request-ID": request_id,
            "X-BoxTeam-Workspace-Id": backend_workspace_id,
            # 按请求注入 Gateway 自身稳定身份，取值与两条 HTTP 代理同源。
            "X-BoxTeam-Gateway-Id": load_proxy_gateway_id(),
        }

    @staticmethod
    def _response_data(response: httpx.Response) -> dict[str, object]:
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise TypeError(
                f"Workspace API 生命周期响应缺少 data: {response.text[:300]}"
            )
        return data

    @staticmethod
    def _parse_blockers(value: object, workspace_id: str | None = None) -> list[GatewayRuntimeBlockerDTO]:
        if not isinstance(value, list):
            raise TypeError("Workspace API 生命周期响应缺少 blockers 数组")
        return [GatewayRuntimeBlockerDTO.model_validate({**item, "workspace_id": workspace_id or item["workspace_id"]}) for item in value]

    def _route_reference_blockers(
        self,
        workspace_id: str,
    ) -> list[GatewayRuntimeBlockerDTO]:
        request_count, stream_count = self._registry.route_reference_counts(
            workspace_id
        )
        blockers: list[GatewayRuntimeBlockerDTO] = []
        if request_count:
            blockers.append(
                GatewayRuntimeBlockerDTO(
                    workspace_id=workspace_id,
                    kind="proxy_request",
                    resource_id=f"{workspace_id}:requests",
                    session_id="gateway-proxy",
                    status="active",
                    detail=f"{request_count} 个代理请求仍在使用旧路由",
                )
            )
        if stream_count:
            blockers.append(
                GatewayRuntimeBlockerDTO(
                    workspace_id=workspace_id,
                    kind="proxy_stream",
                    resource_id=f"{workspace_id}:streams",
                    session_id="gateway-proxy",
                    status="active",
                    detail=f"{stream_count} 个 SSE/WebSocket/流响应仍在使用旧路由",
                )
            )
        return blockers

    async def _delegated_result(
        self, target: WorkspaceTarget, data: dict[str, object], *,
        restart: bool = False, forced: bool = False,
    ) -> GatewayRuntimeStateResultDTO | GatewayRuntimeRestartResultDTO:
        connection_id = target.remote_gateway_connection_id
        if connection_id is None:
            raise RuntimeError("远程生命周期响应缺少 Gateway 连接")
        await refresh_remote_gateway_projections(registry=self._registry, connection_id=connection_id)
        await self._reconcile_registry_dependents()
        by_remote = {
            item.remote_workspace_id: item.workspace_id for item in self._registry.targets()
            if item.remote_gateway_connection_id == connection_id
        }

        def local_id(remote_id: object) -> str:
            if not isinstance(remote_id, str) or remote_id not in by_remote:
                raise RuntimeError(f"远程完整组响应包含未登记成员: {remote_id!r}")
            return by_remote[remote_id]

        raw_affected = data.get("affected_workspace_ids")
        if not isinstance(raw_affected, list):
            raise TypeError("远程完整组响应缺少 affected_workspace_ids")
        affected = [local_id(member_id) for member_id in raw_affected]
        blockers = [
            blocker.model_copy(update={"workspace_id": local_id(blocker.workspace_id)})
            for blocker in self._parse_blockers(data.get("blockers"))
        ]
        result = {
            "workspace_id": target.workspace_id, "status": data["status"],
            "backend_generation": data.get("backend_generation"), "affected_workspace_ids": affected,
            "blockers": blockers,
            "workspaces": GatewayWorkspaceListDTO(
                active_workspace_id=self._registry.active_workspace_id, items=await self._registry.list_dtos(),
            ),
        }
        return GatewayRuntimeRestartResultDTO.model_validate({**result, "forced": forced}) if restart else GatewayRuntimeStateResultDTO.model_validate(result)

    async def reconnect_ssh(self, workspace_id: str) -> None:
        async with self._lock(workspace_id):
            target = self._registry.resolve(workspace_id)
            if (
                target.connection_kind != "remote_gateway"
                or target.remote_gateway_connection_id is None
            ):
                raise ValueError("只有远程 Gateway 工作区可以重新连接 SSH 隧道")
            connection_id = target.remote_gateway_connection_id
            previous_commit = self._registry._last_committed_snapshot
            previous_revision = self._registry.registry_revision
            previous_runtime = self._registry._remote_gateway_runtimes.get(
                connection_id
            )
            try:
                await reconnect_remote_gateway(
                    registry=self._registry,
                    connection_id=connection_id,
                    log_dir=self._log_dir,
                    health_request_timeout_seconds=self._health_request_timeout_seconds,
                    health_poll_interval_seconds=self._health_poll_interval_seconds,
                )
            except BaseException as error:
                current_runtime = self._registry._remote_gateway_runtimes.get(
                    connection_id
                )
                published = (
                    self._registry._last_committed_snapshot is not previous_commit
                    and (
                        self._registry._state_store is None
                        or self._registry.registry_revision > previous_revision
                    )
                    and current_runtime is not None
                    and current_runtime is not previous_runtime
                    and self._registry.remote_gateway_url(connection_id)
                    == current_runtime.service_urls["workspace_api"]
                )
                if not published:
                    raise
                try:
                    await self._reconcile_registry_dependents()
                # 异步对账普通失败附加原错；取消必须传播给请求调用方。
                except Exception as secondary_error:  # noqa: BLE001
                    note = (
                        "远程 Gateway 新 URL 已提交，但依赖资源对账失败: "
                        f"{secondary_error}"
                    )
                    error.add_note(note)
                raise
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
            if target.backend_workspace_id is None:
                raise RuntimeError("外部 target 缺少 Backend UUID binding")
            backend_workspace_id = await asyncio.to_thread(
                resolve_backend_workspace_id,
                self._project_root,
                self._registry.local_workspace_root(workspace_id, target.backend_workspace_id), initialize=False,
            )
            if (
                target.backend_workspace_id is not None
                and target.backend_workspace_id != backend_workspace_id
            ):
                raise RuntimeError(
                    "外部 Workspace backend UUID 与 Gateway registry 不一致: "
                    f"expected={target.backend_workspace_id}, actual={backend_workspace_id}"
                )
            target.backend_workspace_id = backend_workspace_id
            await wait_for_workspace_health(
                f"{target.backend_url.rstrip('/')}/api/v1/health",
                backend_workspace_id,
                request_timeout_seconds=self._health_request_timeout_seconds,
                poll_interval_seconds=self._health_poll_interval_seconds,
            )
            target.connection_error = None
            self._registry.upsert(target, activate=False)

    def _lock(self, workspace_id: str) -> asyncio.Lock:
        shard = zlib.crc32(workspace_id.encode("utf-8")) % _LOCK_SHARDS
        return self._locks[shard]

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
        group_request: GatewayRuntimeGroupRequest | None = None,
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
                json=self._remote_group_request(target, group_request),
                headers={
                    "X-BoxTeam-Federation-Token": credential.token,
                    "X-Request-ID": request_id,
                },
            )
            response.raise_for_status()
        data = self._response_data(response)
        status = data.get("status")
        if status not in {"restarted", "stopped", "blocked"}:
            raise RuntimeError(f"远程 Gateway 返回未知重启状态: {status}")
        return await self._delegated_result(target, data, restart=True, forced=forced)

    async def _delegated_runtime_state(
        self,
        target: WorkspaceTarget,
        action: Literal["start", "stop"],
        *,
        request_id: str,
        group_request: GatewayRuntimeGroupRequest | None = None,
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
                json=self._remote_group_request(target, group_request),
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
        return await self._delegated_result(target, data)

    def _remote_group_request(self, target: WorkspaceTarget, request: GatewayRuntimeGroupRequest | None) -> dict[str, object] | None:
        if request is None:
            return None
        payload = request.model_dump()
        for field in ("expected_workspace_ids", "desired_workspace_ids"):
            ids = payload[field]
            if ids is None:
                continue
            remote_ids = []
            for workspace_id in ids:
                member = self._registry.resolve(workspace_id)
                if member.remote_gateway_connection_id != target.remote_gateway_connection_id or member.remote_workspace_id is None:
                    raise ValueError("远端完整组成员必须属于同一已登记 Gateway 投影")
                remote_ids.append(member.remote_workspace_id)
            payload[field] = remote_ids
        return payload

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
        controller = GatewayWorkspaceRuntimeController(
            registry=registry, project_root=project_root, log_dir=log_dir,
            health_request_timeout_seconds=health_request_timeout_seconds,
            health_poll_interval_seconds=health_poll_interval_seconds,
            connection_drain_timeout_seconds=connection_drain_timeout_seconds,
            default_skill_groups=default_skill_groups,
        )
        owner = registry.backend_owner(workspace_id)
        request = GatewayRuntimeGroupRequest(
            expected_backend_generation=owner.contract.generation,
            expected_workspace_ids=list(owner.contract.workspace_ids),
        ) if owner is not None else None
        await controller.start_managed_backend(workspace_id, request_id=f"reconnect_{uuid4().hex}", group_request=request)
        return
    else:
        if target.backend_workspace_id is None:
            raise RuntimeError("外部 target 缺少 Backend UUID binding")
        backend_workspace_id = await asyncio.to_thread(
            resolve_backend_workspace_id,
            project_root,
            registry.local_workspace_root(workspace_id, target.backend_workspace_id), initialize=False,
        )
        if (
            target.backend_workspace_id is not None
            and target.backend_workspace_id != backend_workspace_id
        ):
            raise RuntimeError(
                "外部 Workspace backend UUID 与 Gateway registry 不一致: "
                f"expected={target.backend_workspace_id}, actual={backend_workspace_id}"
            )
        target.backend_workspace_id = backend_workspace_id
        await wait_for_workspace_health(
            f"{target.backend_url.rstrip('/')}/api/v1/health",
            backend_workspace_id,
            request_timeout_seconds=health_request_timeout_seconds,
            poll_interval_seconds=health_poll_interval_seconds,
        )
        runtime = WorkspaceRuntime(
            service_urls={"workspace_api": target.backend_url},
            backend_workspace_id=backend_workspace_id,
        )

    target.connection_error = None
    registry.upsert(target, runtime=runtime, activate=False)
