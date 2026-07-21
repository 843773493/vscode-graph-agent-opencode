from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx

from app.gateway.credentials import FederationCredential, FederationCredentialStore
from app.gateway.runtime.process import (
    allocate_ssh_tunnel_port,
    start_ssh_tunnel_process,
    wait_for_http_ok,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.service_types import LocalForwardSpec
from app.gateway.ssh_command import build_ssh_command


FEDERATION_PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class RemoteGatewayConnection:
    connection_id: str
    name: str
    host: str
    port: int
    username: str
    private_key_path: str | None
    ssh_config_host: str | None
    remote_gateway_port: int
    remote_gateway_id: str
    protocol_version: int
    connection_error: str | None = None


def build_remote_gateway_connection_id(
    *,
    host: str,
    port: int,
    username: str,
    remote_gateway_port: int,
) -> str:
    digest = hashlib.sha256(
        f"remote-gateway\0{username}@{host}:{port}\0{remote_gateway_port}".encode()
    ).hexdigest()
    return f"rgw_{digest[:32]}"


def build_projected_workspace_id(
    connection_id: str,
    remote_workspace_id: str,
) -> str:
    digest = hashlib.sha256(
        f"projected-workspace\0{connection_id}\0{remote_workspace_id}".encode()
    ).hexdigest()
    return f"gw_{digest[:32]}"


def obtain_pairing_credential_over_ssh(
    *,
    connection_id: str,
    local_gateway_id: str,
    host: str,
    port: int,
    username: str,
    private_key_path: Path | None,
    ssh_config_host: str | None,
) -> FederationCredential:
    pairing_command = os.environ.get(
        "BOXTEAM_REMOTE_PAIR_COMMAND",
        "boxteam gateway issue-federation-token",
    ).strip()
    if not pairing_command:
        raise ValueError("BOXTEAM_REMOTE_PAIR_COMMAND 不能为空")
    remote_command = (
        f"{pairing_command} "
        f"--connection-id {connection_id}:{local_gateway_id} "
        f"--peer-gateway-id {local_gateway_id} --json"
    )
    result = subprocess.run(
        build_ssh_command(
            host=host,
            port=port,
            username=username,
            private_key_path=(
                str(private_key_path) if private_key_path is not None else None
            ),
            ssh_config_host=ssh_config_host,
            remote_command=remote_command,
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "SSH 命令无输出"
        raise RuntimeError(f"远程 Gateway SSH 配对失败: {detail}")
    try:
        payload = json.loads(result.stdout)
        return FederationCredential(
            connection_id=connection_id,
            peer_gateway_id=str(payload["peer_gateway_id"]),
            token=str(payload["token"]),
            expires_at=datetime.fromisoformat(str(payload["expires_at"])),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("远程 Gateway 配对响应不是有效凭据 JSON") from error


async def start_remote_gateway_tunnel(
    *,
    connection: RemoteGatewayConnection,
    log_dir: Path,
) -> WorkspaceRuntime:
    local_port = allocate_ssh_tunnel_port()
    forward = LocalForwardSpec(
        name="workspace_api",
        local_port=local_port,
        remote_host="127.0.0.1",
        remote_port=connection.remote_gateway_port,
    )
    tunnel = start_ssh_tunnel_process(
        host=connection.host,
        port=connection.port,
        username=connection.username,
        private_key_path=(
            Path(connection.private_key_path)
            if connection.private_key_path is not None
            else None
        ),
        ssh_config_host=connection.ssh_config_host,
        forwards=(forward,),
        log_dir=log_dir,
    )
    try:
        await wait_for_http_ok(
            f"{forward.local_url}/api/gateway/health",
            tunnel.process,
        )
    except Exception:
        tunnel.close()
        raise
    return WorkspaceRuntime(
        service_urls={"workspace_api": forward.local_url},
        processes={"ssh_tunnel": tunnel},
    )


async def discover_remote_gateway(
    *,
    gateway_url: str,
    credential: FederationCredential,
    expected_remote_gateway_id: str | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    headers = {
        "X-BoxTeam-Federation-Token": credential.token,
        "X-Request-ID": f"federation-discovery-{credential.connection_id}",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        manifest_response, workspaces_response = await asyncio.gather(
            client.get(f"{gateway_url}/api/gateway/federation/manifest", headers=headers),
            client.get(
                f"{gateway_url}/api/gateway/federation/workspaces",
                headers=headers,
            ),
        )
    manifest_response.raise_for_status()
    workspaces_response.raise_for_status()
    manifest = _response_data(manifest_response)
    workspaces = _response_data(workspaces_response)
    remote_version = manifest.get("protocol_version")
    if remote_version != FEDERATION_PROTOCOL_VERSION:
        raise RuntimeError(
            "远程 Gateway 协议不兼容: "
            f"local={FEDERATION_PROTOCOL_VERSION}, remote={remote_version}"
        )
    remote_gateway_id = manifest.get("gateway_id")
    if not isinstance(remote_gateway_id, str):
        raise RuntimeError("远程 Gateway manifest 缺少 gateway_id")
    if expected_remote_gateway_id and remote_gateway_id != expected_remote_gateway_id:
        raise RuntimeError(
            "远程 Gateway 身份发生变化: "
            f"expected={expected_remote_gateway_id}, actual={remote_gateway_id}"
        )
    if remote_gateway_id == credential.peer_gateway_id:
        raise RuntimeError("拒绝连接自身 Gateway，联邦连接会形成循环")
    raw_items = workspaces.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("远程 Gateway workspaces 响应缺少 items 数组")
    direct_items: list[dict[str, object]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise RuntimeError("远程 Gateway workspace 项必须是对象")
        if item.get("connection_kind") != "local":
            continue
        direct_items.append(item)
    return manifest, direct_items


async def request_remote_gateway_management(
    *,
    gateway_url: str,
    credential: FederationCredential,
    method: Literal["GET", "POST", "DELETE"],
    path: str,
    request_id: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    headers = {
        "X-BoxTeam-Federation-Token": credential.token,
        "X-Request-ID": request_id,
    }
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.request(
            method,
            f"{gateway_url.rstrip('/')}{path}",
            headers=headers,
            json=payload,
        )
    response.raise_for_status()
    return _response_data(response)


def _response_data(response: httpx.Response) -> dict[str, object]:
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError(f"远程 Gateway 响应缺少 data: {response.text[:300]}")
    return data
