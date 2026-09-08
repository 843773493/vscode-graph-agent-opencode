from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.core.path_utils import get_gateway_root
from app.gateway.config import ConfiguredRemoteGateway, resolve_gateway_path
from app.gateway.credentials import (
    FederationCredentialStore,
    load_or_create_gateway_id,
)
from app.gateway.federation import (
    FEDERATION_PROTOCOL_VERSION,
    RemoteGatewayConnection,
    build_projected_workspace_id,
    build_remote_gateway_connection_id,
    discover_remote_gateway,
    obtain_pairing_credential_over_ssh,
    start_remote_gateway_tunnel,
)
from app.gateway.registry import (
    GatewayRegistryBatchHandle,
    GatewayWorkspaceRegistry,
    WorkspaceTarget,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeConsumerStage,
    GatewayRuntimeConsumerTransaction,
    GatewayRuntimeHealthProof,
)


def _synchronize_projected_workspaces(
    *,
    registry: GatewayWorkspaceRegistry,
    connection: RemoteGatewayConnection,
    gateway_url: str,
    remote_workspaces: list[dict[str, object]],
    remote_config_event_cursor: int | None,
    activate: bool,
    preserve_custom_names: bool,
    runtime: WorkspaceRuntime | None = None,
) -> tuple[WorkspaceTarget, ...]:
    registry.validate_remote_projection_cursor(
        connection.connection_id,
        remote_config_event_cursor,
    )
    projected: list[WorkspaceTarget] = []
    for remote in remote_workspaces:
        remote_workspace_id = str(remote["workspace_id"])
        workspace_id = build_projected_workspace_id(
            connection.connection_id,
            remote_workspace_id,
        )
        existing = (
            registry.resolve(workspace_id)
            if registry.has_target(workspace_id)
            else None
        )
        keep_existing_name = bool(
            preserve_custom_names and existing and existing.name_customized
        )
        effective_cursor = (
            remote_config_event_cursor
            if remote_config_event_cursor is not None
            else (
                existing.remote_config_event_cursor
                if existing is not None
                else None
            )
        )
        target = WorkspaceTarget(
            workspace_id=workspace_id,
            name=existing.name if keep_existing_name and existing else str(remote["name"]),
            name_customized=existing.name_customized if keep_existing_name and existing else False,
            root_path=str(remote["root_path"]),
            backend_url=gateway_url,
            connection_kind="remote_gateway",
            owner="remote_projection",
            connection_id=connection.connection_id,
            managed=bool(remote.get("managed", False)),
            remote_gateway_connection_id=connection.connection_id,
            remote_workspace_id=remote_workspace_id,
            remote_config_event_cursor=effective_cursor,
            remote_service_names=tuple(remote.get("services", ("workspace_api",))),
            connection_error=None,
        )
        projected.append(target)
    registry.apply_remote_projection_snapshot(
        connection=connection,
        projections=tuple(projected),
        runtime=runtime,
        activate=activate,
    )
    return tuple(projected)


def _projected_workspace_targets(
    *,
    registry: GatewayWorkspaceRegistry,
    connection: RemoteGatewayConnection,
    gateway_url: str,
    remote_workspaces: list[dict[str, object]],
    remote_config_event_cursor: int | None,
    preserve_custom_names: bool,
) -> tuple[WorkspaceTarget, ...]:
    """只构造投影 target，不修改 registry，供 config batch 做原子提交。"""

    projected: list[WorkspaceTarget] = []
    for remote in remote_workspaces:
        remote_workspace_id = str(remote["workspace_id"])
        workspace_id = build_projected_workspace_id(
            connection.connection_id,
            remote_workspace_id,
        )
        existing = (
            registry.resolve(workspace_id)
            if registry.has_target(workspace_id)
            else None
        )
        keep_existing_name = bool(
            preserve_custom_names and existing is not None and existing.name_customized
        )
        effective_cursor = (
            remote_config_event_cursor
            if remote_config_event_cursor is not None
            else (
                existing.remote_config_event_cursor
                if existing is not None
                else None
            )
        )
        projected.append(
            WorkspaceTarget(
                workspace_id=workspace_id,
                name=(
                    existing.name
                    if keep_existing_name and existing is not None
                    else str(remote["name"])
                ),
                name_customized=(
                    existing.name_customized
                    if keep_existing_name and existing is not None
                    else False
                ),
                root_path=str(remote["root_path"]),
                backend_url=gateway_url,
                connection_kind="remote_gateway",
                owner="remote_projection",
                connection_id=connection.connection_id,
                managed=bool(remote.get("managed", False)),
                remote_gateway_connection_id=connection.connection_id,
                remote_workspace_id=remote_workspace_id,
                remote_config_event_cursor=effective_cursor,
                remote_service_names=tuple(
                    str(service)
                    for service in remote.get("services", ("workspace_api",))
                ),
                connection_error=None,
            )
        )
    return tuple(projected)


async def _prepare_remote_gateway(
    *,
    log_dir: Path,
    configured: ConfiguredRemoteGateway,
    health_request_timeout_seconds: float,
    health_poll_interval_seconds: float,
    source_owner: Literal["config", "manual"] = "config",
) -> tuple[RemoteGatewayConnection, WorkspaceRuntime, list[dict[str, object]]]:
    """建立并验证远程 tunnel；成功前不触碰本地 registry。"""

    resolved_private_key_path = (
        resolve_gateway_path(configured.private_key_path)
        if configured.private_key_path
        else None
    )
    if resolved_private_key_path is not None and not resolved_private_key_path.is_file():
        raise FileNotFoundError(f"SSH 私钥不存在: {resolved_private_key_path}")
    if resolved_private_key_path is None and not configured.ssh_config_host:
        raise ValueError("显式 SSH 连接必须提供 private_key_path")
    connection_id = configured.connection_id
    if not connection_id:
        raise ValueError("已校验的 Gateway 远程配置缺少 connection_id")
    gateway_root = get_gateway_root()
    local_gateway_id = load_or_create_gateway_id(gateway_root / "identity.json")
    credential_store = FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    )
    credential = await asyncio.to_thread(
        obtain_pairing_credential_over_ssh,
        connection_id=connection_id,
        local_gateway_id=local_gateway_id,
        host=configured.host,
        port=configured.port,
        username=configured.username,
        private_key_path=resolved_private_key_path,
        ssh_config_host=configured.ssh_config_host,
        remote_pair_command=configured.remote_pair_command,
    )
    credential_store.put(credential)
    provisional = RemoteGatewayConnection(
        connection_id=connection_id,
        name=configured.name or configured.host,
        host=configured.host,
        port=configured.port,
        username=configured.username,
        private_key_path=(
            str(resolved_private_key_path)
            if resolved_private_key_path is not None
            else None
        ),
        ssh_config_host=configured.ssh_config_host,
        remote_gateway_port=configured.remote_gateway_port,
        remote_gateway_id="pending",
        protocol_version=FEDERATION_PROTOCOL_VERSION,
        remote_pair_command=configured.remote_pair_command,
        source_owner=source_owner,
    )
    runtime = await start_remote_gateway_tunnel(
        connection=provisional,
        log_dir=log_dir,
        health_request_timeout_seconds=health_request_timeout_seconds,
        health_poll_interval_seconds=health_poll_interval_seconds,
    )
    try:
        manifest, remote_workspaces = await discover_remote_gateway(
            gateway_url=runtime.service_urls["workspace_api"],
            credential=credential,
        )
        if not remote_workspaces:
            raise RuntimeError("远程 Gateway 没有可导入的直接管理工作区")
        if int(manifest["protocol_version"]) != FEDERATION_PROTOCOL_VERSION:
            raise RuntimeError("远程 Gateway 联邦协议版本不匹配")
        connection = RemoteGatewayConnection(
            connection_id=provisional.connection_id,
            name=provisional.name,
            host=provisional.host,
            port=provisional.port,
            username=provisional.username,
            private_key_path=provisional.private_key_path,
            ssh_config_host=provisional.ssh_config_host,
            remote_gateway_port=provisional.remote_gateway_port,
            remote_gateway_id=str(manifest["gateway_id"]),
            protocol_version=provisional.protocol_version,
            remote_pair_command=provisional.remote_pair_command,
            source_owner=provisional.source_owner,
            remote_config_event_cursor=_manifest_config_event_cursor(manifest),
            remote_config_state=_manifest_config_state(manifest),
            remote_restart_required=_manifest_restart_required(manifest),
            remote_candidate_ref=_manifest_candidate_ref(manifest),
        )
        return connection, runtime, remote_workspaces
    except Exception:
        runtime.close()
        raise


async def register_remote_gateway(
    *,
    registry: GatewayWorkspaceRegistry,
    log_dir: Path,
    name: str | None,
    host: str,
    port: int,
    username: str,
    private_key_path: str | None,
    ssh_config_host: str | None,
    remote_gateway_port: int,
    connection_id: str | None = None,
    remote_pair_command: str | None = None,
    activate: bool = False,
    health_request_timeout_seconds: float = 2,
    health_poll_interval_seconds: float = 0.5,
) -> tuple[WorkspaceTarget, ...]:
    configured = ConfiguredRemoteGateway(
        name=name,
        host=host,
        port=port,
        username=username,
        private_key_path=private_key_path or "",
        connection_id=connection_id
        or build_remote_gateway_connection_id(
            host=host,
            port=port,
            username=username,
            remote_gateway_port=remote_gateway_port,
        ),
        ssh_config_host=ssh_config_host,
        remote_pair_command=remote_pair_command,
        remote_gateway_port=remote_gateway_port,
        activate=activate,
    )
    connection, runtime, remote_workspaces = await _prepare_remote_gateway(
        log_dir=log_dir,
        configured=configured,
        health_request_timeout_seconds=health_request_timeout_seconds,
        health_poll_interval_seconds=health_poll_interval_seconds,
        source_owner="manual",
    )
    try:
        gateway_url = runtime.service_urls["workspace_api"]
        return _synchronize_projected_workspaces(
            registry=registry,
            connection=connection,
            gateway_url=gateway_url,
            remote_workspaces=remote_workspaces,
            remote_config_event_cursor=connection.remote_config_event_cursor,
            activate=activate,
            preserve_custom_names=False,
            runtime=runtime,
        )
    except Exception:
        runtime.close()
        raise


async def reconcile_configured_remote_gateways(
    *,
    registry: GatewayWorkspaceRegistry,
    configured_workspaces: tuple[ConfiguredRemoteGateway, ...],
    log_dir: Path,
    health_request_timeout_seconds: float,
    health_poll_interval_seconds: float,
) -> int:
    """准备全部 remote projection 后，以一个 registry CAS batch 切换。"""

    if not configured_workspaces and not any(
        connection.source_owner == "config"
        for connection in registry.remote_gateway_connections()
    ):
        return 0
    if len({item.connection_id for item in configured_workspaces}) != len(
        configured_workspaces
    ):
        raise ValueError("Gateway 配置 batch 包含重复 connection_id")
    staged_connections: list[RemoteGatewayConnection] = []
    staged_runtimes: dict[str, WorkspaceRuntime] = {}
    staged_projections: dict[str, tuple[WorkspaceTarget, ...]] = {}
    activate_connection_ids: list[str] = []
    registry_batch_handle: GatewayRegistryBatchHandle | None = None
    generation = f"gateway-remote-projection:{uuid4().hex}"

    async def prepare_remote_projection() -> None:
        try:
            for configured in configured_workspaces:
                connection, runtime, remote_workspaces = await _prepare_remote_gateway(
                    log_dir=log_dir,
                    configured=configured,
                    health_request_timeout_seconds=health_request_timeout_seconds,
                    health_poll_interval_seconds=health_poll_interval_seconds,
                )
                staged_connections.append(connection)
                staged_runtimes[connection.connection_id] = runtime
                staged_projections[connection.connection_id] = (
                    _projected_workspace_targets(
                        registry=registry,
                        connection=connection,
                        gateway_url=runtime.service_urls["workspace_api"],
                        remote_workspaces=remote_workspaces,
                        remote_config_event_cursor=connection.remote_config_event_cursor,
                        preserve_custom_names=True,
                    )
                )
                if configured.activate:
                    activate_connection_ids.append(connection.connection_id)
        except BaseException:
            for runtime in staged_runtimes.values():
                runtime.close()
            staged_connections.clear()
            staged_runtimes.clear()
            staged_projections.clear()
            activate_connection_ids.clear()
            raise

    def assert_staged_runtime_healthy(runtime: object) -> None:
        assert_healthy = getattr(runtime, "assert_healthy", None)
        if callable(assert_healthy):
            assert_healthy()
            return
        service_urls = getattr(runtime, "service_urls", None)
        if not isinstance(service_urls, dict) or not service_urls:
            raise RuntimeError("远程 projection staged runtime 缺少服务地址")
        if any(
            not isinstance(url, str) or not url.strip()
            for url in service_urls.values()
        ):
            raise RuntimeError("远程 projection staged runtime 服务地址为空")

    def remote_projection_proof() -> GatewayRuntimeHealthProof:
        for runtime in staged_runtimes.values():
            assert_staged_runtime_healthy(runtime)
        return GatewayRuntimeHealthProof(
            consumer_id="remote-projection",
            generation=generation,
            state="healthy",
            details={"staged_connection_count": len(staged_connections)},
        )

    def apply_remote_projection() -> None:
        # prepare 阶段建立私有 tunnel；apply 阶段再次确认它们仍然可用，
        # 在 registry CAS 前不向任何投影路由暴露未验证的 runtime。
        for runtime in staged_runtimes.values():
            assert_staged_runtime_healthy(runtime)

    def rollback_remote_projection() -> None:
        for runtime in staged_runtimes.values():
            runtime.close()

    def prepare_registry_batch() -> None:
        if len(staged_connections) != len(staged_runtimes):
            raise RuntimeError("Gateway remote projection staged runtime 集合不完整")
        if {connection.connection_id for connection in staged_connections} != set(
            staged_projections
        ):
            raise RuntimeError("Gateway remote projection staged projection 集合不完整")

    def apply_registry_batch() -> None:
        nonlocal registry_batch_handle
        registry_batch_handle = registry.apply_remote_projection_batch(
            connections=tuple(staged_connections),
            runtimes=staged_runtimes,
            projections=staged_projections,
            activate_connection_ids=tuple(activate_connection_ids),
            defer_retiring_runtimes=True,
        )

    def registry_batch_proof() -> GatewayRuntimeHealthProof:
        return registry.runtime_health_proof(
            consumer_id="registry-batch",
            generation=generation,
        )

    def promote_registry_batch() -> None:
        if registry_batch_handle is None:
            raise RuntimeError("Gateway registry batch promotion 缺少 batch handle")
        registry_batch_handle.promote()

    def rollback_registry_batch() -> None:
        if registry_batch_handle is not None:
            registry_batch_handle.rollback()

    transaction = GatewayRuntimeConsumerTransaction(
        (
            GatewayRuntimeConsumerStage(
                consumer_id="remote-projection",
                generation=generation,
                prepare=prepare_remote_projection,
                apply=apply_remote_projection,
                health=remote_projection_proof,
                promote=lambda: None,
                rollback=rollback_remote_projection,
            ),
            GatewayRuntimeConsumerStage(
                consumer_id="registry-batch",
                generation=generation,
                prepare=prepare_registry_batch,
                apply=apply_registry_batch,
                health=registry_batch_proof,
                promote=promote_registry_batch,
                rollback=rollback_registry_batch,
            ),
        )
    )
    result = await transaction.apply()
    try:
        await result.promote()
    except BaseException:
        await result.rollback()
        raise
    return len(staged_connections)


async def reconnect_remote_gateway(
    *,
    registry: GatewayWorkspaceRegistry,
    connection_id: str,
    log_dir: Path,
    health_request_timeout_seconds: float = 2,
    health_poll_interval_seconds: float = 0.5,
) -> tuple[WorkspaceTarget, ...]:
    connection = registry.remote_gateway_connection(connection_id)
    gateway_root = get_gateway_root()
    credential_store = FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    )
    credential = await asyncio.to_thread(
        obtain_pairing_credential_over_ssh,
        connection_id=connection_id,
        local_gateway_id=load_or_create_gateway_id(gateway_root / "identity.json"),
        host=connection.host,
        port=connection.port,
        username=connection.username,
        private_key_path=(
            Path(connection.private_key_path)
            if connection.private_key_path is not None
            else None
        ),
        ssh_config_host=connection.ssh_config_host,
        remote_pair_command=connection.remote_pair_command,
    )
    credential_store.put(credential)
    runtime = await start_remote_gateway_tunnel(
        connection=connection,
        log_dir=log_dir,
        health_request_timeout_seconds=health_request_timeout_seconds,
        health_poll_interval_seconds=health_poll_interval_seconds,
    )
    try:
        manifest, remote_workspaces = await discover_remote_gateway(
            gateway_url=runtime.service_urls["workspace_api"],
            credential=credential,
            expected_remote_gateway_id=connection.remote_gateway_id,
        )
        if not remote_workspaces:
            raise RuntimeError("远程 Gateway 没有可导入的直接管理工作区")
        if int(manifest["protocol_version"]) != connection.protocol_version:
            raise RuntimeError("远程 Gateway 持久化协议版本与当前响应不一致")
        connection = _connection_with_manifest_state(connection, manifest)
        return _synchronize_projected_workspaces(
            registry=registry,
            connection=connection,
            gateway_url=runtime.service_urls["workspace_api"],
            remote_workspaces=remote_workspaces,
            remote_config_event_cursor=connection.remote_config_event_cursor,
            activate=False,
            preserve_custom_names=True,
            runtime=runtime,
        )
    except Exception:
        runtime.close()
        raise


async def refresh_remote_gateway_projections(
    *,
    registry: GatewayWorkspaceRegistry,
    connection_id: str,
) -> tuple[WorkspaceTarget, ...]:
    connection = registry.remote_gateway_connection(connection_id)
    credential = FederationCredentialStore(
        storage_path=get_gateway_root() / "credentials" / "federation.json"
    ).get(connection_id)
    gateway_url = registry.remote_gateway_url(connection_id)
    manifest, remote_workspaces = await discover_remote_gateway(
        gateway_url=gateway_url,
        credential=credential,
        expected_remote_gateway_id=connection.remote_gateway_id,
    )
    if int(manifest["protocol_version"]) != connection.protocol_version:
        raise RuntimeError("远程 Gateway 持久化协议版本与当前响应不一致")
    connection = _connection_with_manifest_state(connection, manifest)
    return _synchronize_projected_workspaces(
        registry=registry,
        connection=connection,
        gateway_url=gateway_url,
        remote_workspaces=remote_workspaces,
        remote_config_event_cursor=connection.remote_config_event_cursor,
        activate=False,
        preserve_custom_names=True,
    )


def _manifest_config_event_cursor(manifest: dict[str, object]) -> int | None:
    value = manifest.get("config_event_cursor")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("远程 Gateway manifest config_event_cursor 无效")
    return value


def _manifest_config_state(manifest: dict[str, object]) -> str | None:
    value = manifest.get("config_reload_state")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RuntimeError("远程 Gateway manifest config_reload_state 无效")
    return value


def _manifest_restart_required(manifest: dict[str, object]) -> bool:
    value = manifest.get("config_reload_restart_required", False)
    if not isinstance(value, bool):
        raise RuntimeError(
            "远程 Gateway manifest config_reload_restart_required 无效"
        )
    return value


def _manifest_candidate_ref(manifest: dict[str, object]) -> str | None:
    value = manifest.get("config_reload_candidate_ref")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RuntimeError("远程 Gateway manifest config_reload_candidate_ref 无效")
    return value


def _connection_with_manifest_state(
    connection: RemoteGatewayConnection,
    manifest: dict[str, object],
) -> RemoteGatewayConnection:
    return replace(
        connection,
        remote_config_event_cursor=_manifest_config_event_cursor(manifest),
        remote_config_state=_manifest_config_state(manifest),
        remote_restart_required=_manifest_restart_required(manifest),
        remote_candidate_ref=_manifest_candidate_ref(manifest),
    )


def _remove_stale_projections(
    *,
    registry: GatewayWorkspaceRegistry,
    connection_id: str,
    current_workspace_ids: set[str],
) -> None:
    stale_workspace_ids = [
        target.workspace_id
        for target in registry.targets()
        if target.remote_gateway_connection_id == connection_id
        and target.workspace_id not in current_workspace_ids
    ]
    for workspace_id in stale_workspace_ids:
        registry.remove(workspace_id, owner="remote_projection")
