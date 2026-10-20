"""固定 URI 的只读导航路由；正文与文件系统访问留给目标 owner。"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError

from app.core.resource_uri import (
    ResourceUriGrammarError,
    parse_resource_uri,
    resource_display_uri,
)
from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.control.user_access import UserAccessContext
from app.gateway.federation.errors import (
    FEDERATION_AUTHORIZATION_REVOKED,
    FEDERATION_CAPABILITY_DENIED,
    FEDERATION_HARDENING_ACTIVE,
    FEDERATION_MALFORMED_FRAME,
    FEDERATION_TARGET_AMBIGUOUS,
    FEDERATION_TARGET_NOT_RESOLVABLE,
    FederationError,
)
from app.gateway.federation.grants import ResourceNavigationGrantTarget
from app.gateway.federation.messages import FEDERATION_DEADLINE_SECONDS
from app.gateway.server.workspace_proxy import (
    _proxy_workspace_request,
    _registry,
    verify_user_access_for_proxy,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.communication import ResourceNavigationRelayRequest
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.services.infrastructure.resource_platform.sources.gateway_resource_navigation import (
    qualify_navigation_result,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)

router = APIRouter()


async def _navigate(
    payload: ResourceNavigationRequest, request: Request, *, auth: GatewayAuthContext | None,
    user_access: UserAccessContext | None, backend_workspace_id: str | None = None,
) -> Response:
    gateway_id = request.app.state.gateway_id
    try:
        if auth is not None and auth.kind == "federation":
            raise HTTPException(400, detail="联邦资源导航必须通过原 read grant relay")
        resource = parse_resource_uri(payload.uri, local_gateway_id=gateway_id)
        payload = payload.model_copy(update={"uri": resource.display_uri})
        if backend_workspace_id is not None:
            targets = [target for target in _registry(request).targets() if target.backend_workspace_id == backend_workspace_id]
            if not targets:
                raise HTTPException(404, detail="workspace 未注册或不存在")
            # TODO：同一后端注册到多个 Gateway 的固定工作区路由需按显式 owner 消歧；当前重复目标仍明确拒绝。
            if len(targets) != 1:
                raise FederationError(FEDERATION_TARGET_AMBIGUOUS, "多个 Gateway route 指向同一导航 workspace")
            workspace_target = targets[0]
            owner_gateway_id = gateway_id
            if workspace_target.connection_kind == "remote_gateway":
                connection_id = workspace_target.remote_gateway_connection_id
                if connection_id is None:
                    raise RuntimeError("远程导航 Workspace 缺少 Gateway 连接身份")
                owner_gateway_id = _registry(request).remote_gateway_connection(connection_id).remote_gateway_id
            if resource.gateway_authority not in {None, owner_gateway_id} or (resource.scope == "workspace" and resource.scope_id != backend_workspace_id):
                raise ResourceUriResolveError("scope_mismatch", "导航 URI 与固定 Workspace 路由不一致")
            if workspace_target.connection_kind != "remote_gateway":
                return await _proxy_workspace_request(
                    f"workspaces/{backend_workspace_id}/resources/navigation", request,
                    auth=auth, user_access=user_access, include_credentials=True,
                    request_body=payload.model_dump_json(exclude_unset=True).encode("utf-8"),
                )
            # 固定远端 Workspace 入口同样走签名 read grant，不再转发联邦 HTTP 凭据。
            uri = resource_display_uri(scope=resource.scope, scope_id=resource.scope_id, kind=resource.kind,
                                       tail_segments=resource.tail_segments, gateway_authority=owner_gateway_id)
            resource = parse_resource_uri(uri, local_gateway_id=gateway_id)
            payload = payload.model_copy(update={"uri": uri})
        if resource.gateway_authority is None and resource.scope == "workspace":
            targets = [target for target in _registry(request).targets() if target.connection_kind == "local" and target.backend_workspace_id == resource.scope_id]
            if not targets:
                raise ResourceUriResolveError("unknown_resource", "workspace 未注册或不存在")
            if len(targets) != 1:
                raise FederationError(FEDERATION_TARGET_AMBIGUOUS, "多个本地 Gateway route 指向同一导航 workspace")
            return await _proxy_workspace_request(
                "resources/navigation", request, auth=auth, user_access=user_access,
                include_credentials=True, target_workspace_id=targets[0].workspace_id,
                request_body=payload.model_dump_json(exclude_unset=True).encode("utf-8"),
            )
        if resource.gateway_authority is None:
            if resource.scope != "gateway" or resource.scope_id != gateway_id:
                raise ResourceUriResolveError("scope_mismatch", "此导航 scope 需要显式 Workspace 路由")
            if resource.kind == "files":
                # 资源固定属于本机 Gateway；按稳定身份选择同机执行进程，不读取激活态。
                targets = sorted((target for target in _registry(request).targets()
                                  if target.connection_kind == "local" and target.backend_url.strip()
                                  and target.backend_workspace_id is not None),
                                 key=lambda target: target.backend_workspace_id)
                if not targets:
                    raise ResourceUriResolveError("unknown_resource", "原生目录没有可用的同机文件 owner")
                return await _proxy_workspace_request(
                    "resources/navigation", request, auth=auth, user_access=user_access,
                    include_credentials=True, target_workspace_id=targets[0].workspace_id,
                    request_body=payload.model_dump_json(exclude_unset=True).encode("utf-8"),
                )
            result = await request.app.state.skill_source_owner.navigate(payload)
        else:
            if resource.scope not in {"workspace", "gateway"}:
                raise ResourceUriResolveError("scope_mismatch", "远端导航 scope 需要明确的目标 owner")
            target = ResourceNavigationGrantTarget(gateway_id=resource.gateway_authority, uri=resource.display_uri)
            envelope = ResourceNavigationRelayRequest(source_gateway_id=gateway_id, navigation_target=target, navigation=payload)
            data = await request.app.state.federation_rpc_service.relay_operation(
                target=target, operation="read", body=envelope.model_dump(mode="json"),
                deadline_at=time.time() + min(FEDERATION_DEADLINE_SECONDS, payload.timeout_ms / 1000),
            )
            while isinstance(data.get("result"), dict) and "relayed_by" in data:
                data = data["result"]
            try:
                result = ResourceNavigationResultDTO.model_validate(data)
            except ValidationError as error:
                raise FederationError(FEDERATION_MALFORMED_FRAME, "导航响应不符合目标 owner 合同") from error
            result = qualify_navigation_result(result, uri=payload.uri, gateway_id=target.gateway_id)
            if len(result.model_dump_json().encode("utf-8")) > payload.max_response_bytes:
                raise ResourceFileOperationError("output_limit_exceeded", "限定 Gateway 后的导航响应超过总字节上限", reason="response_byte_limit")
    except FederationError as error:
        if error.code in {FEDERATION_CAPABILITY_DENIED, FEDERATION_HARDENING_ACTIVE, FEDERATION_TARGET_NOT_RESOLVABLE} or (error.code == FEDERATION_AUTHORIZATION_REVOKED and error.detail.get("capability") == "read"):
            error = FederationError(FEDERATION_TARGET_NOT_RESOLVABLE, "导航资源未共享或不存在")
        raise HTTPException(409, detail=error.to_wire()) from error
    except ResourceUriGrammarError as error:
        raise HTTPException(400, detail={"owner": "resource_uri_grammar", "code": error.reason_code, "message": str(error)}) from error
    except ResourceUriResolveError as error:
        raise HTTPException(404 if error.reason_code == "unknown_resource" else 403, detail={"owner": "resource_uri_resolver", "code": error.reason_code, "message": str(error)}) from error
    except ResourceFileOperationError as error:
        raise HTTPException(400, detail=error.detail()) from error
    except (OSError, UnicodeError) as error:
        raise HTTPException(400, detail={"owner": "resource_navigation", "type": type(error).__name__, "errno": getattr(error, "errno", None)}) from error
    request_id = get_request_id(request)
    content = APIResponse(data=result, request_id=request_id).model_dump_json()
    if len(content.encode("utf-8")) > payload.max_response_bytes:
        raise HTTPException(400, detail=ResourceFileOperationError(
            "output_limit_exceeded", "导航响应及请求回执超过总字节上限", reason="response_byte_limit",
        ).detail())
    return Response(content=content, media_type="application/json", headers={"X-Request-ID": request_id})


@router.post("/api/v1/resources/navigation", response_model=APIResponse[ResourceNavigationResultDTO])
async def navigate_resource(
    payload: ResourceNavigationRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    user_access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> Response:
    return await _navigate(payload, request, auth=auth, user_access=user_access)


@router.post("/api/v1/workspaces/{backend_workspace_id}/resources/navigation", response_model=APIResponse[ResourceNavigationResultDTO])
async def navigate_workspace_resource(
    backend_workspace_id: str, payload: ResourceNavigationRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    user_access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> Response:
    return await _navigate(payload, request, auth=auth, user_access=user_access, backend_workspace_id=backend_workspace_id)


@router.post("/api/gateway/resource-navigation/workspaces/{source_workspace_id}",
             response_model=APIResponse[ResourceNavigationResultDTO], dependencies=[Depends(verify_gateway_token)])
async def navigate_from_workspace(
    source_workspace_id: str, payload: ResourceNavigationRequest, request: Request,
) -> Response:
    """已注册本机 Workspace 的服务调用；不伪造浏览器登录态。"""
    if not any(target.connection_kind == "local" and target.backend_workspace_id == source_workspace_id
               for target in _registry(request).targets()):
        raise HTTPException(403, detail="文件导航来源不是当前 Gateway 的本机 Workspace")
    return await _navigate(payload, request, auth=None, user_access=None)
