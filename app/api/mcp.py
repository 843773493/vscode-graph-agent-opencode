from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_mcp_runtime_manager, get_request_id, verify_local_token
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.mcp import (
    McpServerDTO,
    McpToolDTO,
)
from app.services.infrastructure.mcp import McpRuntimeManager


router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get(
    "/servers",
    response_model=APIResponse[list[McpServerDTO]],
    summary="获取 MCP Server 与工具目录",
)
async def list_mcp_servers(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    manager: McpRuntimeManager = Depends(get_mcp_runtime_manager),
):
    servers = [
        McpServerDTO(
            server_id=server.server_id,
            transport=server.transport,
            enabled=server.enabled,
            status=server.status,
            tools=[
                McpToolDTO(
                    tool_id=tool.tool_id,
                    server_id=tool.server_id,
                    remote_name=tool.remote_name,
                    description=tool.description,
                )
                for tool in server.tools
            ],
        )
        for server in manager.list_servers()
    ]
    return APIResponse(data=servers, request_id=request_id)
