"""向客户端投影唯一资源地址解析，不读取工作区业务状态。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.resource_uri import (
    ResourceUriGrammarError,
    file_uri_root,
    parse_resource_uri,
    parse_session_thread_uri,
    resource_display_uri,
)
from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.schemas.gateway.resource_address import (
    GatewayResourceAddressDTO,
    GatewayResourceAddressRequest,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter(dependencies=[Depends(verify_gateway_token)])


@router.post("/api/gateway/resource-addresses/parse", response_model=APIResponse[GatewayResourceAddressDTO])
def parse_resource_address(
    body: GatewayResourceAddressRequest, request: Request,
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[GatewayResourceAddressDTO]:
    if body.local_gateway_id != request.app.state.gateway_id:
        raise HTTPException(409, detail="发起打开的 Gateway 身份已改变，请重新打开资源")
    try:
        resource_gateway_id = body.resource_gateway_id or request.app.state.gateway_id
        parsed = parse_resource_uri(body.uri, local_gateway_id=resource_gateway_id)
        if body.resource_gateway_id and parsed.gateway_authority is not None:
            raise HTTPException(409, detail={"owner": "resource_uri_grammar", "code": "resource_owner_mismatch",
                                            "message": "URI 的 Gateway 与明确来源归属不一致"})
        session_id = thread_id = None
        if parsed.kind == "session" and parsed.tail_segments and parsed.session_artifact_kind is None:
            _, session_id, thread_id = parse_session_thread_uri(parsed.display_uri)
        return APIResponse(data=GatewayResourceAddressDTO(
            uri=parsed.display_uri,
            reference_uri=resource_display_uri(
                scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
                tail_segments=parsed.tail_segments,
                gateway_authority=parsed.gateway_authority or resource_gateway_id,
            ),
            gateway_id=parsed.gateway_authority or resource_gateway_id,
            scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
            file_root_uri=file_uri_root(parsed.display_uri) if parsed.kind in {"files", "attachments"} else None,
            session_id=session_id, thread_id=thread_id,
        ), request_id=request_id)
    except ResourceUriGrammarError as error:
        raise HTTPException(400, detail={"owner": "resource_uri_grammar", "code": error.reason_code, "message": str(error)}) from error
