"""Backend 进程级宿主入口；只接受 Gateway 私有控制通道。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from app.api.runtime_logging import verify_runtime_control_token
from app.container import WorkspaceContainerRegistry
from app.core.trace_middleware import get_request_id
from app.schemas.gateway.extension_host import (
    ExtensionHostConnectionDTO,
    ExtensionHostOpenRequest,
    ExtensionHostRuntimeDTO,
    ExtensionHostTargetDTO,
    ExtensionHostTargetRequest,
)
from app.schemas.gateway.extension_installation import (
    ExtensionInstallationInstallDTO,
    ExtensionInstallationInstallRequest,
    ExtensionInstallationInventoryDTO,
    ExtensionInstallationInventoryRequest,
)
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.extension_host.channel import (
    close_channel,
    run_channel,
)
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
)
from app.services.infrastructure.extension_host.owner import NodeHostConnection

router = APIRouter()
logger = logging.getLogger(__name__)


def host_registry(connection: HTTPConnection, _: str = Depends(verify_runtime_control_token)) -> WorkspaceContainerRegistry:
    if (connection.client is None or connection.client.host not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        or "origin" in connection.headers or "sec-fetch-site" in connection.headers):
        raise HTTPException(403, "Node 宿主只接受本机 Gateway 管理通道")
    registry = getattr(connection.app.state, "workspace_container_registry", None)
    if not isinstance(registry, WorkspaceContainerRegistry):
        raise TypeError("Backend 工作区注册表尚未初始化")
    return registry


@router.post("/api/internal/extension-hosts/resolve", response_model=APIResponse[ExtensionHostTargetDTO], include_in_schema=False)
async def resolve_extension_host(
    payload: ExtensionHostTargetRequest, request: Request,
    registry: Annotated[WorkspaceContainerRegistry, Depends(host_registry)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionHostTargetDTO]:
    if request.headers.get("x-boxteam-gateway-id") != payload.gateway_id:
        raise HTTPException(400, "Node 宿主目标 Gateway 身份冲突")
    try:
        status = registry.extension_hosts.describe(expected_backend_id=payload.backend_id, workspace_ids=tuple(payload.workspace_ids))
    except (ValueError, KeyError) as error:
        raise HTTPException(409, str(error)) from error
    runtime = status.runtime
    result = ExtensionHostTargetDTO(
        **payload.model_dump(exclude={"backend_id"}), backend_id=status.backend_id, platform=status.platform,
        channel_state="draining" if status.reason == "backend_draining" else "unavailable" if status.reason else "available",
        reason=status.reason, runtime=ExtensionHostRuntimeDTO(commit=runtime.commit, vscode_version=runtime.vscode_version,
            node_version=runtime.node_version) if runtime is not None else None)
    return APIResponse(data=result, request_id=request_id)


@router.post("/api/internal/extension-targets/resolve", response_model=APIResponse[ExtensionInstallationInventoryRequest], include_in_schema=False)
async def resolve_extension_target(
    payload: ExtensionHostTargetRequest, request: Request,
    registry: Annotated[WorkspaceContainerRegistry, Depends(host_registry)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionInstallationInventoryRequest]:
    if request.headers.get("x-boxteam-gateway-id") != payload.gateway_id:
        raise HTTPException(400, "扩展目标 Gateway 身份冲突")
    try:
        status = registry.extension_hosts.describe(expected_backend_id=payload.backend_id,
            workspace_ids=tuple(payload.workspace_ids))
    except (ValueError, KeyError) as error:
        raise HTTPException(409, str(error)) from error
    result = ExtensionInstallationInventoryRequest(protocol_version=payload.protocol_version,
        gateway_id=payload.gateway_id, workspace_ids=payload.workspace_ids, backend_id=status.backend_id)
    return APIResponse(data=result, request_id=request_id)


@router.post("/api/internal/extension-installations/install", response_model=APIResponse[ExtensionInstallationInstallDTO], include_in_schema=False)
async def extension_installation_install(
    payload: ExtensionInstallationInstallRequest, request: Request,
    registry: Annotated[WorkspaceContainerRegistry, Depends(host_registry)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionInstallationInstallDTO]:
    if request.headers.get("x-boxteam-gateway-id") != payload.gateway_id:
        raise HTTPException(400, "扩展安装目标 Gateway 身份冲突")
    try:
        result = await registry.extension_hosts.install(payload)
    except (ValueError, KeyError) as error:
        raise HTTPException(409, str(error)) from error
    except ExtensionManagementError as error:
        raise HTTPException(400, str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/api/internal/extension-installations/inventory", response_model=APIResponse[ExtensionInstallationInventoryDTO], include_in_schema=False)
async def extension_installation_inventory(
    payload: ExtensionInstallationInventoryRequest, request: Request,
    registry: Annotated[WorkspaceContainerRegistry, Depends(host_registry)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionInstallationInventoryDTO]:
    if request.headers.get("x-boxteam-gateway-id") != payload.gateway_id:
        raise HTTPException(400, "扩展库存目标 Gateway 身份冲突")
    owner = registry.extension_hosts

    def check_target() -> None:
        try:
            status = owner.describe(expected_backend_id=payload.backend_id, workspace_ids=tuple(payload.workspace_ids))
        except (ValueError, KeyError) as error:
            raise HTTPException(409, str(error)) from error
        if status.reason is not None:
            raise HTTPException(409, f"目标扩展库存不可用: {status.reason}")

    check_target()
    inventory = await owner.management.inventory()
    check_target()
    result = ExtensionInstallationInventoryDTO(**payload.model_dump(exclude={"backend_id"}),
        backend_id=owner.backend_id, inventory=inventory)
    return APIResponse(data=result, request_id=request_id)


async def _receive(websocket: WebSocket, connection: NodeHostConnection) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is None:
            raise ValueError("宿主握手后只接受原协议二进制帧")
        if connection.stopping.is_set():
            return
        connection.writer.write(data)
        await connection.writer.drain()


async def _send(websocket: WebSocket, connection: NodeHostConnection, binding: ExtensionHostConnectionDTO) -> None:
    await websocket.send_text(binding.model_dump_json())
    while data := await connection.reader.read(65536):
        await websocket.send_bytes(data)


async def _stopped(connection: NodeHostConnection) -> None:
    await connection.stopping.wait()


@router.websocket("/api/internal/extension-hosts/channel")
async def extension_host_channel(websocket: WebSocket, registry: Annotated[WorkspaceContainerRegistry, Depends(host_registry)]) -> None:
    request_id = get_request_id(websocket)
    gateway_id = websocket.headers.get("x-boxteam-gateway-id")
    client_id = websocket.headers.get("x-boxteam-extension-client-id")
    if not gateway_id or not client_id:
        await websocket.send_denial_response(JSONResponse(status_code=400, content={
            "detail": "Node 宿主缺少 Gateway 认证身份", "request_id": request_id}))
        return
    await websocket.accept()
    try:
        async with asyncio.timeout(10):
            payload = ExtensionHostOpenRequest.model_validate_json(await websocket.receive_text())
        if payload.gateway_id != gateway_id:
            raise ValueError("Node 宿主目标 Gateway 身份冲突")
        async with registry.extension_hosts.connect(gateway_id=gateway_id, expected_backend_id=payload.backend_id,
                client_id=client_id, view_id=payload.view_id, workspace_ids=tuple(payload.workspace_ids)) as connection:
            identity = asdict(connection.identity)
            identity["workspace_ids"] = list(connection.identity.workspace_ids)
            result = ExtensionHostConnectionDTO(protocol_version=1, request_id=request_id,
                initialization=connection.initialization, **identity)
            # 首次await前登记传输占用；owner关闭IPC前必须等双方任务实际结清。
            connection.transport_settled.clear()
            try:
                await run_channel(_receive(websocket, connection), _send(websocket, connection, result), _stopped(connection))
            finally:
                connection.transport_settled.set()
        await close_channel(websocket)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("Backend Node 宿主通道失败: request_id=%s", request_id)
        await close_channel(websocket, code=1011, reason="extension_host_channel_failed")
        raise
