from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import ValidationError

from app.api.deps import (
    get_mcp_catalog_owner,
    get_mcp_connection_service,
    get_request_id,
    verify_local_token,
)
from app.core.trace_middleware import get_current_gateway_id
from app.schemas.internal_v2.mcp import (
    McpAuthorizationCallbackDTO,
    McpAuthorizationResponseDTO,
    McpConnectionOperationListResponseDTO,
    McpConnectionOperationResponseDTO,
    McpConnectionRequestDTO,
    McpServerDTO,
    McpServerListResponseDTO,
    McpToolDTO,
    McpWorkspaceTargetDTO,
)
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogRelistError
from app.services.infrastructure.mcp.connection_service import McpConnectionService

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _target(service: McpConnectionService) -> McpWorkspaceTargetDTO:
    return McpWorkspaceTargetDTO(gateway_id=get_current_gateway_id(), workspace_id=service.workspace_id)


@router.get(
    "/servers",
    response_model=McpServerListResponseDTO,
    summary="获取 MCP Server 与工具目录",
)
async def list_mcp_servers(
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    try:
        await owner.relist_servers_without_notifications()
    except McpCatalogRelistError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    snapshot = owner.management_snapshot()
    servers = [
        McpServerDTO(
            server_id=server.server_id,
            transport=server.transport,
            enabled=server.enabled,
            status=server.status,
            connection_ref=server.connection_ref,
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
        for server in snapshot.servers
    ]
    return McpServerListResponseDTO(
        data=servers, request_id=request_id, target=_target(service),
        catalog_revision=snapshot.catalog_revision, catalog_generation=snapshot.catalog_generation,
    )


@contextmanager
def _connection_errors():
    try:
        yield
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValidationError:
        raise
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except TimeoutError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/connection-operations", response_model=McpConnectionOperationListResponseDTO)
async def list_connection_operations(
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    return McpConnectionOperationListResponseDTO(data=service.list_operations(), request_id=request_id, target=_target(service))


@router.post("/connection-operations", response_model=McpConnectionOperationResponseDTO)
async def create_connection_operation(
    body: McpConnectionRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    with _connection_errors():
        return McpConnectionOperationResponseDTO(data=await service.submit(body), request_id=request_id, target=_target(service))


@router.get("/connection-operations/{operation_id}", response_model=McpConnectionOperationResponseDTO)
async def get_connection_operation(
    operation_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
    wait_timeout_ms: Annotated[int, Query(ge=0, le=5000)] = 0,
):
    with _connection_errors():
        return McpConnectionOperationResponseDTO(
            data=await service.status(operation_id, wait_timeout_ms=wait_timeout_ms), request_id=request_id, target=_target(service),
        )


@router.post("/connection-operations/{operation_id}/cancel", response_model=McpConnectionOperationResponseDTO)
async def cancel_connection_operation(
    operation_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    with _connection_errors():
        return McpConnectionOperationResponseDTO(data=await service.cancel(operation_id), request_id=request_id, target=_target(service))


@router.get("/connection-operations/{operation_id}/authorization", response_model=McpAuthorizationResponseDTO)
async def get_connection_authorization(
    operation_id: str,
    response: Response,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    response.headers["Cache-Control"] = "no-store"
    with _connection_errors():
        return McpAuthorizationResponseDTO(data=service.authorization(operation_id), request_id=request_id, target=_target(service))


@router.post("/connection-operations/{operation_id}/callback", response_model=McpConnectionOperationResponseDTO)
async def complete_connection_authorization(
    operation_id: str,
    body: McpAuthorizationCallbackDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    with _connection_errors():
        return McpConnectionOperationResponseDTO(
            data=service.complete_authorization(operation_id, code=body.code, state=body.state), request_id=request_id, target=_target(service),
        )
