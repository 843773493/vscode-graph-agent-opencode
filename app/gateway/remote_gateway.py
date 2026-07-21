from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.path_utils import get_gateway_root
from app.gateway.config import resolve_gateway_path
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
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget


def _synchronize_projected_workspaces(
    *,
    registry: GatewayWorkspaceRegistry,
    connection: RemoteGatewayConnection,
    gateway_url: str,
    remote_workspaces: list[dict[str, object]],
    activate: bool,
    preserve_custom_names: bool,
) -> tuple[WorkspaceTarget, ...]:
    registry.upsert_remote_gateway(connection)
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
        target = WorkspaceTarget(
            workspace_id=workspace_id,
            name=existing.name if keep_existing_name and existing else str(remote["name"]),
            name_customized=existing.name_customized if keep_existing_name and existing else False,
            root_path=str(remote["root_path"]),
            backend_url=gateway_url,
            connection_kind="remote_gateway",
            managed=bool(remote.get("managed", False)),
            remote_gateway_connection_id=connection.connection_id,
            remote_workspace_id=remote_workspace_id,
            remote_service_names=tuple(remote.get("services", ("workspace_api",))),
            connection_error=None,
        )
        registry.upsert(target, activate=activate and not projected)
        projected.append(target)
    _remove_stale_projections(
        registry=registry,
        connection_id=connection.connection_id,
        current_workspace_ids={item.workspace_id for item in projected},
    )
    return tuple(projected)


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
    activate: bool = False,
) -> tuple[WorkspaceTarget, ...]:
    resolved_private_key_path = (
        resolve_gateway_path(private_key_path) if private_key_path else None
    )
    if resolved_private_key_path is not None and not resolved_private_key_path.is_file():
        raise FileNotFoundError(f"SSH 私钥不存在: {resolved_private_key_path}")
    if resolved_private_key_path is None and not ssh_config_host:
        raise ValueError("显式 SSH 连接必须提供 private_key_path")
    connection_id = build_remote_gateway_connection_id(
        host=host,
        port=port,
        username=username,
        remote_gateway_port=remote_gateway_port,
    )
    gateway_root = get_gateway_root()
    local_gateway_id = load_or_create_gateway_id(gateway_root / "identity.json")
    credential_store = FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    )
    # 每次建立新 SSH 隧道都重新签发凭据，避免远端 Gateway 重装后继续使用
    # 本地残留 token；SSH 主机认证仍是配对信任根。
    credential = await asyncio.to_thread(
        obtain_pairing_credential_over_ssh,
        connection_id=connection_id,
        local_gateway_id=local_gateway_id,
        host=host,
        port=port,
        username=username,
        private_key_path=resolved_private_key_path,
        ssh_config_host=ssh_config_host,
    )
    credential_store.put(credential)

    provisional = RemoteGatewayConnection(
        connection_id=connection_id,
        name=name or host,
        host=host,
        port=port,
        username=username,
        private_key_path=(
            str(resolved_private_key_path)
            if resolved_private_key_path is not None
            else None
        ),
        ssh_config_host=ssh_config_host,
        remote_gateway_port=remote_gateway_port,
        remote_gateway_id="pending",
        protocol_version=FEDERATION_PROTOCOL_VERSION,
    )
    runtime = await start_remote_gateway_tunnel(
        connection=provisional,
        log_dir=log_dir,
    )
    try:
        gateway_url = runtime.service_urls["workspace_api"]
        manifest, remote_workspaces = await discover_remote_gateway(
            gateway_url=gateway_url,
            credential=credential,
        )
        if not remote_workspaces:
            raise RuntimeError("远程 Gateway 没有可导入的直接管理工作区")
        remote_gateway_id = str(manifest["gateway_id"])
        connection = RemoteGatewayConnection(
            connection_id=connection_id,
            name=name or host,
            host=host,
            port=port,
            username=username,
            private_key_path=provisional.private_key_path,
            ssh_config_host=ssh_config_host,
            remote_gateway_port=remote_gateway_port,
            remote_gateway_id=remote_gateway_id,
            protocol_version=FEDERATION_PROTOCOL_VERSION,
        )
        registry.upsert_remote_gateway(connection, runtime=runtime)
        return _synchronize_projected_workspaces(
            registry=registry,
            connection=connection,
            gateway_url=gateway_url,
            remote_workspaces=remote_workspaces,
            activate=activate,
            preserve_custom_names=False,
        )
    except Exception:
        runtime.close()
        raise


async def reconnect_remote_gateway(
    *,
    registry: GatewayWorkspaceRegistry,
    connection_id: str,
    log_dir: Path,
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
    )
    credential_store.put(credential)
    runtime = await start_remote_gateway_tunnel(
        connection=connection,
        log_dir=log_dir,
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
        registry.upsert_remote_gateway(connection, runtime=runtime)
        return _synchronize_projected_workspaces(
            registry=registry,
            connection=connection,
            gateway_url=runtime.service_urls["workspace_api"],
            remote_workspaces=remote_workspaces,
            activate=False,
            preserve_custom_names=True,
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
    return _synchronize_projected_workspaces(
        registry=registry,
        connection=connection,
        gateway_url=gateway_url,
        remote_workspaces=remote_workspaces,
        activate=False,
        preserve_custom_names=True,
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
        registry.remove(workspace_id)
