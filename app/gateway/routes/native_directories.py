"""原生目录根绑定控制入口；文件执行仍归 Workspace 文件 owner。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.resource_uri import resource_display_uri
from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import get_registry
from app.schemas.gateway.native_directories import (
    NativeDirectoryBindingDTO,
    NativeDirectoryBindRequest,
    NativeDirectoryResolveDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter(dependencies=[Depends(verify_gateway_token)])


def _require_native_owner(request: Request, registry: GatewayWorkspaceRegistry, workspace_id: str) -> None:
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "原生根绑定只提供给同机文件 owner")
    if not any(target.connection_kind == "local" and target.backend_workspace_id == workspace_id
               for target in registry.targets()):
        raise HTTPException(403, "文件 owner 不属于本 Gateway 的本机工作区")


@router.post("/api/gateway/native-directories/workspaces/{workspace_id}", response_model=APIResponse[NativeDirectoryBindingDTO])
def bind_native_directory(
    workspace_id: str, body: NativeDirectoryBindRequest, request: Request,
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[NativeDirectoryBindingDTO]:
    _require_native_owner(request, registry, workspace_id)
    gateway_id = request.app.state.gateway_id
    directory_id = request.app.state.gateway_state.bind_native_directory(gateway_id=gateway_id, native_root=body.native_root)
    uri = resource_display_uri(scope="gateway", scope_id=gateway_id, kind="files", tail_segments=(directory_id,))
    return APIResponse(data=NativeDirectoryBindingDTO(uri=uri), request_id=request_id)


@router.get("/api/gateway/native-directories/workspaces/{workspace_id}/{gateway_id}/{directory_id}", response_model=APIResponse[NativeDirectoryResolveDTO])
def resolve_native_directory(
    workspace_id: str, gateway_id: str, directory_id: str, request: Request,
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[NativeDirectoryResolveDTO]:
    _require_native_owner(request, registry, workspace_id)
    if gateway_id != request.app.state.gateway_id:
        raise HTTPException(409, "原生目录 Gateway owner 不符")
    try:
        native_root = request.app.state.gateway_state.resolve_native_directory(gateway_id=gateway_id, directory_id=directory_id)
    except KeyError as error:
        raise HTTPException(404, "原生目录根绑定不存在") from error
    uri = resource_display_uri(scope="gateway", scope_id=gateway_id, kind="files", tail_segments=(directory_id,))
    return APIResponse(data=NativeDirectoryResolveDTO(uri=uri, native_root=native_root), request_id=request_id)
