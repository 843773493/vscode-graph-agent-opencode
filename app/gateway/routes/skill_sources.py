"""向本 Gateway 已登记的本机 Workspace 发布认证原始 Skill 来源。"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.federation.errors import FederationError
from app.gateway.federation.skill_sources import (
    GatewaySkillSourcePort,
    relay_gateway_skill_sources,
)
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import get_registry
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.schemas.gateway.skill_sources import GatewaySkillSourcesDTO
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    NAVIGATION_DEADLINE_HEADER,
    ResourceFileOperationError,
    ResourceNavigation,
    navigation_deadline,
    navigation_deadline_at,
)

router = APIRouter()


def _get_skill_source_owner(request: Request) -> GatewaySkillSourceOwner:
    return request.app.state.skill_source_owner


async def _source_request(
    *, request: Request, registry: GatewayWorkspaceRegistry, owner: GatewaySkillSourceOwner,
    gateway_id: str, workspace_id: str, request_id: str, kind: str,
    if_none_match: str | None = None, navigation: ResourceNavigationRequest | None = None,
    resolve_owner: bool = False,
    deadline: float | None = None,
) -> dict[str, object]:
    port = GatewaySkillSourcePort(owner=owner, registry=registry)
    payload = {
        "gateway_id": gateway_id, "workspace_id": workspace_id,
        "request_id": request_id, "kind": kind, "if_none_match": if_none_match,
        "navigation": navigation.model_dump(mode="json") if navigation is not None else None,
    }
    if gateway_id == owner.gateway_id:
        result = await port.execute(payload, deadline=deadline)
    else:
        if not resolve_owner:
            raise HTTPException(status_code=409, detail="Gateway Skill source owner mismatch")
        if not port.accepts_workspace(workspace_id):
            raise HTTPException(403, detail="Workspace is outside Gateway Skill source audience")
        try:
            result = await relay_gateway_skill_sources(request.app.state.federation_rpc_service, payload,
                deadline_at=navigation_deadline_at(deadline) if deadline is not None else None)
        except FederationError as error:
            raise HTTPException(status_code=409, detail=error.to_wire()) from error
    status = result.get("status")
    if status not in {200, 304}:
        if status not in {400, 403, 404, 409}:
            raise ValueError("Gateway Skill source 回执 status 无效")
        raise HTTPException(status_code=status, detail=result.get("detail"))
    return result


def _snapshot_response(
    result: dict[str, object], response: Response, request_id: str,
) -> APIResponse[GatewaySkillSourcesDTO] | Response:
    etag = result.get("etag")
    if not isinstance(etag, str) or not etag:
        raise ValueError("Gateway Skill source 回执缺少 ETag")
    if result["status"] == 304:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
    return APIResponse(data=GatewaySkillSourcesDTO.model_validate(result["data"]), request_id=request_id)


def _navigation_response(result: dict[str, object], body: ResourceNavigationRequest,
                         request_id: str) -> APIResponse[ResourceNavigationResultDTO]:
    envelope = APIResponse(data=ResourceNavigationResultDTO.model_validate(result["data"]), request_id=request_id)
    if len(envelope.model_dump_json().encode("utf-8")) > body.max_response_bytes:
        raise HTTPException(400, detail=ResourceFileOperationError(
            "output_limit_exceeded", "导航响应及请求回执超过总字节上限", reason="response_byte_limit",
        ).detail())
    return envelope


async def _navigate_source(
    *, body: ResourceNavigationRequest, request: Request, registry: GatewayWorkspaceRegistry,
    owner: GatewaySkillSourceOwner, gateway_id: str, workspace_id: str, request_id: str,
    resolve_owner: bool = False,
) -> APIResponse[ResourceNavigationResultDTO]:
    try:
        deadline = navigation_deadline(body, deadline_at=request.headers.get(NAVIGATION_DEADLINE_HEADER))
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            result = await _source_request(request=request, registry=registry, owner=owner,
                gateway_id=gateway_id, workspace_id=workspace_id, request_id=request_id,
                kind="navigation", navigation=body, resolve_owner=resolve_owner, deadline=deadline)
            envelope = _navigation_response(result, body, request_id)
            ResourceNavigation._check_deadline(deadline)
            return envelope
    except TimeoutError as error:
        raise HTTPException(400, detail=ResourceFileOperationError(
            "output_limit_exceeded", "文件导航超过本次总期限", reason="deadline",
        ).detail()) from error
    except ResourceFileOperationError as error:
        raise HTTPException(400, detail=error.detail()) from error


@router.post(
    "/api/gateway/skill-sources/{gateway_id}/workspaces/{workspace_id}/navigation",
    response_model=APIResponse[ResourceNavigationResultDTO],
    dependencies=[Depends(verify_gateway_token)],
)
async def navigate_skill_source(
    gateway_id: str, workspace_id: str, body: ResourceNavigationRequest,
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    owner: Annotated[GatewaySkillSourceOwner, Depends(_get_skill_source_owner)],
    request: Request,
    request_id: str = Depends(get_request_id),
) -> APIResponse[ResourceNavigationResultDTO]:
    return await _navigate_source(
        request=request, registry=registry, owner=owner, gateway_id=gateway_id,
        workspace_id=workspace_id, request_id=request_id, body=body,
    )


@router.get(
    "/api/gateway/skill-sources/{gateway_id}/workspaces/{workspace_id}",
    response_model=APIResponse[GatewaySkillSourcesDTO],
    dependencies=[Depends(verify_gateway_token)],
)
async def skill_sources(
    gateway_id: str, workspace_id: str, response: Response,
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    owner: Annotated[GatewaySkillSourceOwner, Depends(_get_skill_source_owner)],
    request: Request,
    request_id: str = Depends(get_request_id),
    if_none_match: str | None = Header(default=None),
) -> APIResponse[GatewaySkillSourcesDTO] | Response:
    result = await _source_request(
        request=request, registry=registry, owner=owner, gateway_id=gateway_id,
        workspace_id=workspace_id, request_id=request_id, kind="snapshot", if_none_match=if_none_match,
    )
    return _snapshot_response(result, response, request_id)


@router.post(
    "/api/gateway/skill-source-connections/{gateway_id}/workspaces/{workspace_id}/navigation",
    response_model=APIResponse[ResourceNavigationResultDTO],
    dependencies=[Depends(verify_gateway_token)],
)
async def navigate_connected_skill_source(
    gateway_id: str, workspace_id: str, body: ResourceNavigationRequest,
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    owner: Annotated[GatewaySkillSourceOwner, Depends(_get_skill_source_owner)],
    request: Request,
    request_id: str = Depends(get_request_id),
) -> APIResponse[ResourceNavigationResultDTO]:
    return await _navigate_source(
        request=request, registry=registry, owner=owner, gateway_id=gateway_id,
        workspace_id=workspace_id, request_id=request_id, body=body,
        resolve_owner=True,
    )


@router.get(
    "/api/gateway/skill-source-connections/{gateway_id}/workspaces/{workspace_id}",
    response_model=APIResponse[GatewaySkillSourcesDTO],
    dependencies=[Depends(verify_gateway_token)],
)
async def connected_skill_sources(
    gateway_id: str, workspace_id: str, response: Response,
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    owner: Annotated[GatewaySkillSourceOwner, Depends(_get_skill_source_owner)],
    request: Request,
    request_id: str = Depends(get_request_id),
    if_none_match: str | None = Header(default=None),
) -> APIResponse[GatewaySkillSourcesDTO] | Response:
    result = await _source_request(
        request=request, registry=registry, owner=owner, gateway_id=gateway_id,
        workspace_id=workspace_id, request_id=request_id, kind="snapshot", if_none_match=if_none_match,
        resolve_owner=True,
    )
    return _snapshot_response(result, response, request_id)
