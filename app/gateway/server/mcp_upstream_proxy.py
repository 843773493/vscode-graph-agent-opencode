"""把外部 Streamable HTTP MCP 请求从目标 Gateway 转发到配置端点。"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.runtime_control import verify_local_control_request
from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.proxy_upstream import (
    GATEWAY_PROXY_DROPPED_HEADERS,
    UpstreamResponseBody,
    filter_hop_by_hop_headers,
    send_upstream_request,
)

router = APIRouter(dependencies=[Depends(verify_gateway_token), Depends(verify_local_control_request)])
MCP_ENDPOINT_HEADER = "X-BoxTeam-MCP-Endpoint"


@router.api_route("/api/gateway/mcp/upstream", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def proxy_mcp_upstream(
    request: Request,
) -> StreamingResponse:
    endpoint = request.headers.get(MCP_ENDPOINT_HEADER)
    if not endpoint:
        raise HTTPException(400, f"缺少 {MCP_ENDPOINT_HEADER}")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise HTTPException(400, "外部 MCP endpoint 必须是无凭据的 HTTP(S) URL")

    client = request.app.state.http_client
    headers = filter_hop_by_hop_headers(
        (name, value)
        for name, value in request.headers.items()
        if name.lower() not in GATEWAY_PROXY_DROPPED_HEADERS
        and name.lower() != MCP_ENDPOINT_HEADER.lower()
    )
    headers["X-Request-ID"] = get_request_id(request)
    url = httpx.URL(endpoint).copy_merge_params(tuple(request.query_params.multi_items()))
    upstream_request = client.build_request(
        request.method,
        url,
        headers=headers,
        content=request.stream() if request.method == "POST" else None,
    )
    try:
        response = await send_upstream_request(client, upstream_request)
    except httpx.HTTPError as error:
        raise HTTPException(502, f"目标 Gateway 无法连接外部 MCP endpoint: {type(error).__name__}") from error
    except TimeoutError as error:
        raise HTTPException(504, "目标 Gateway 等待外部 MCP endpoint 响应头超时") from error

    response_headers = filter_hop_by_hop_headers(
        (name, value)
        for name, value in response.headers.items()
        if name.lower() not in {"content-length", "content-encoding", "set-cookie", "x-request-id"}
    )
    response_headers["X-Request-ID"] = get_request_id(request)
    return StreamingResponse(
        UpstreamResponseBody(response),
        status_code=response.status_code,
        headers=response_headers,
        background=None,
    )
