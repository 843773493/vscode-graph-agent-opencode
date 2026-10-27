from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import (
    get_mcp_catalog_owner,
    get_mcp_connection_service,
    get_request_id,
    verify_local_token,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.mcp import (
    McpAuthorizationCallbackDTO,
    McpAuthorizationDTO,
    McpConnectionOperationDTO,
    McpConnectionRequestDTO,
    McpServerDTO,
    McpToolDTO,
)
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogRelistError
from app.services.infrastructure.mcp.connection_service import McpConnectionService

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


@contextmanager
def _connection_errors():
    try:
        yield
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/connection-operations", response_model=APIResponse[McpConnectionOperationDTO])
async def create_connection_operation(
    body: McpConnectionRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    with _connection_errors():
        return APIResponse(data=await service.submit(body), request_id=request_id)


@router.get("/connection-operations/{operation_id}", response_model=APIResponse[McpConnectionOperationDTO])
async def get_connection_operation(
    operation_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
    wait_timeout_ms: Annotated[int, Query(ge=0, le=5000)] = 0,
):
    with _connection_errors():
        return APIResponse(data=await service.status(operation_id, wait_timeout_ms=wait_timeout_ms), request_id=request_id)


@router.post("/connection-operations/{operation_id}/cancel", response_model=APIResponse[McpConnectionOperationDTO])
async def cancel_connection_operation(
    operation_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    with _connection_errors():
        return APIResponse(data=await service.cancel(operation_id), request_id=request_id)


@router.get("/connection-operations/{operation_id}/authorization", response_model=APIResponse[McpAuthorizationDTO])
async def get_connection_authorization(
    operation_id: str,
    response: Response,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    response.headers["Cache-Control"] = "no-store"
    with _connection_errors():
        return APIResponse(data=service.authorization(operation_id), request_id=request_id)


@router.post("/connection-operations/{operation_id}/callback", response_model=APIResponse[McpConnectionOperationDTO])
async def complete_connection_authorization(
    operation_id: str,
    body: McpAuthorizationCallbackDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    with _connection_errors():
        return APIResponse(data=service.complete_authorization(operation_id, code=body.code, state=body.state), request_id=request_id)
