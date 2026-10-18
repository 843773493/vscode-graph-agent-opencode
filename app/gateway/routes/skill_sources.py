"""向本 Gateway 已登记的本机 Workspace 发布认证原始 Skill 来源。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import get_registry
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.schemas.gateway.skill_sources import GatewaySkillSourcesDTO
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


def _get_skill_source_owner(request: Request) -> GatewaySkillSourceOwner:
    return request.app.state.skill_source_owner


@router.get(
    "/api/gateway/skill-sources/{gateway_id}/workspaces/{workspace_id}",
    response_model=APIResponse[GatewaySkillSourcesDTO],
    dependencies=[Depends(verify_gateway_token)],
)
async def skill_sources(
    gateway_id: str, workspace_id: str, response: Response,
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    owner: Annotated[GatewaySkillSourceOwner, Depends(_get_skill_source_owner)],
    request_id: str = Depends(get_request_id),
    if_none_match: str | None = Header(default=None),
) -> APIResponse[GatewaySkillSourcesDTO] | Response:
    if gateway_id != owner.gateway_id:
        raise HTTPException(status_code=409, detail="Gateway Skill source owner mismatch")
    if not any(
        target.connection_kind == "local" and target.backend_workspace_id == workspace_id
        for target in registry.targets()
    ):
        raise HTTPException(status_code=403, detail="Workspace is outside Gateway Skill source audience")
    snapshot = owner.snapshot(workspace_id=workspace_id)
    etag = '"' + snapshot.revision + '"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
    return APIResponse(data=snapshot, request_id=request_id)
