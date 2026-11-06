"""Backend 进程级宿主入口；只接受 Gateway 私有控制通道。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from starlette.responses import JSONResponse

from app.api.runtime_logging import verify_runtime_control_token
from app.container import WorkspaceContainerRegistry
from app.core.trace_middleware import get_request_id
from app.schemas.gateway.extension_host import (
    ExtensionHostConnectionDTO,
    ExtensionHostOpenRequest,
)
from app.services.infrastructure.extension_host.channel import (
    close_channel,
    run_channel,
)
from app.services.infrastructure.extension_host.owner import NodeHostConnection

router = APIRouter()
logger = logging.getLogger(__name__)


def host_registry(websocket: WebSocket, _: str = Depends(verify_runtime_control_token)) -> WorkspaceContainerRegistry:
    if (websocket.client is None or websocket.client.host not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        or "origin" in websocket.headers or "sec-fetch-site" in websocket.headers):
        raise HTTPException(403, "Node 宿主只接受本机 Gateway 管理通道")
    registry = getattr(websocket.app.state, "workspace_container_registry", None)
    if not isinstance(registry, WorkspaceContainerRegistry):
        raise TypeError("Backend 工作区注册表尚未初始化")
    return registry


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
            result = ExtensionHostConnectionDTO(protocol_version=1, request_id=request_id, **identity)
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
