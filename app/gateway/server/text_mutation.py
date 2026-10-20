"""文本传输入口的鉴权与透明代理；文件业务仍归工作区 owner。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.control.user_access import UserAccessContext
from app.gateway.server.workspace_proxy import (
    _proxy_workspace_request,
    verify_user_access_for_proxy,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.text_mutation import (
    TextMutationRequest,
    TextMutationTransferDTO,
)

router = APIRouter()


@router.post("/api/v1/resources/text-mutations", response_model=APIResponse[TextMutationTransferDTO])
@router.post("/api/v1/workspaces/{backend_workspace_id}/resources/text-mutations", response_model=APIResponse[TextMutationTransferDTO])
async def mutate_workspace_text(
    payload: TextMutationRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    user_access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
    backend_workspace_id: str | None = None,
):
    if auth.kind == "federation":
        raise HTTPException(400, detail="联邦文本修改必须通过独立 file_write grant relay")
    path = "resources/text-mutations" if backend_workspace_id is None else f"workspaces/{backend_workspace_id}/resources/text-mutations"
    return await _proxy_workspace_request(path, request, auth=auth, user_access=user_access,
        include_credentials=True, request_body=payload.model_dump_json(exclude_none=True).encode("utf-8"))
