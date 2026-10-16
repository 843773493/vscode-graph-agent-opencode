from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_mcp_catalog_owner, get_request_id, verify_local_token
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.mcp import (
    McpServerDTO,
    McpToolDTO,
)
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogRelistError

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get(
    "/servers",
    response_model=APIResponse[list[McpServerDTO]],
    summary="获取 MCP Server 与工具目录",
)
async def list_mcp_servers(
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
):
    try:
        await owner.relist_servers_without_notifications()
    except McpCatalogRelistError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
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
        for server in owner.list_servers()
    ]
    return APIResponse(data=servers, request_id=request_id)
