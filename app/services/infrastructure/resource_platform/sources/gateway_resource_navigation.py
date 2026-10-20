"""文件工具经已配置 Gateway 导航；联邦选路及执行仍归原 owner。"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from app.core.resource_uri import (
    ResourceUriGrammarError,
    parse_resource_uri,
    resource_display_uri,
)
from app.core.trace_middleware import require_current_request_id
from app.gateway.federation.errors import (
    FEDERATION_ERROR_CODES,
    FEDERATION_MALFORMED_FRAME,
    FederationError,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceFileOperationError,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
    resource_navigation_node,
)


def qualify_navigation_result(
    result: ResourceNavigationResultDTO, *, uri: str, gateway_id: str,
) -> ResourceNavigationResultDTO:
    """各边界共用归属校验；文件匹配必须属于已返回节点及原请求根。"""
    root = parse_resource_uri(uri, local_gateway_id=gateway_id)
    try:
        resource_navigation_node(root, resource_shape=result.node.resource_shape)
    except ResourceUriResolveError as error:
        raise FederationError(FEDERATION_MALFORMED_FRAME, "导航响应的节点形态不符合资源根合同") from error

    def qualify_uri(value: str, *, child: bool) -> str:
        try:
            resource = parse_resource_uri(value, local_gateway_id=gateway_id)
        except ResourceUriGrammarError as error:
            raise FederationError(FEDERATION_MALFORMED_FRAME, "导航响应包含非法 URI") from error
        if resource.gateway_authority is not None or (resource.scope, resource.scope_id, resource.kind) != (root.scope, root.scope_id, root.kind):
            raise FederationError(FEDERATION_MALFORMED_FRAME, "导航响应改变资源归属")
        tail = resource.tail_segments
        same_file = result.node.resource_shape == "file" and tail == root.tail_segments
        if (not child and tail != root.tail_segments) or (child and not same_file and (len(tail) <= len(root.tail_segments) or tail[:len(root.tail_segments)] != root.tail_segments)):
            raise FederationError(FEDERATION_MALFORMED_FRAME, "导航响应越出请求根")
        return resource_display_uri(scope=resource.scope, scope_id=resource.scope_id, kind=resource.kind,
            tail_segments=tail, gateway_authority=gateway_id)

    file_uris = {node.uri for node in result.entries if node.resource_shape == "file"}
    if any(match.uri not in file_uris for match in result.matches):
        raise FederationError(FEDERATION_MALFORMED_FRAME, "搜索匹配没有对应的文件节点")
    return result.model_copy(update={
        "node": result.node.model_copy(update={"uri": qualify_uri(result.node.uri, child=False)}),
        "entries": tuple(node.model_copy(update={"uri": qualify_uri(node.uri, child=True)}) for node in result.entries),
        "matches": tuple(match.model_copy(update={"uri": qualify_uri(match.uri, child=True)}) for match in result.matches),
    })


class GatewayResourceNavigation:
    """只传输固定 URI，不接受宿主路径、endpoint 或激活工作区。"""

    def __init__(self, *, workspace_id: str, connection: GatewayTransportConnection, local_token_supplier: Callable[[], str]) -> None:
        self._workspace_id = workspace_id
        self._connection, self._local_token_supplier = connection, local_token_supplier

    def _request(self, request: ResourceNavigationRequest) -> tuple[str, httpx.Request]:
        parsed = parse_resource_uri(request.uri)
        if parsed.gateway_authority is None or parsed.scope not in {"workspace", "gateway"}:
            raise ResourceUriResolveError("scope_mismatch", "Gateway 文件传输必须固定目标 authority 和资源根")
        request_id = require_current_request_id("Gateway 文件导航")
        outgoing = httpx.Request("POST", self._connection.resolve_gateway_url()
            + f"/api/gateway/resource-navigation/workspaces/{self._workspace_id}",
            headers={"X-Local-Token": self._local_token_supplier(), "X-Request-ID": request_id},
            json=request.model_dump(), extensions={"timeout": httpx.Timeout(min(
                self._connection.resolve_timeout_seconds(), request.timeout_ms / 1000,
            )).as_dict()})
        return parsed.gateway_authority, outgoing

    def navigate_sync(self, request: ResourceNavigationRequest) -> ResourceNavigationResultDTO:
        gateway_id, outgoing = self._request(request)
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            response = client.send(outgoing)
        return self._response(response, request, outgoing.headers["X-Request-ID"], gateway_id)

    async def navigate(self, request: ResourceNavigationRequest) -> ResourceNavigationResultDTO:
        gateway_id, outgoing = self._request(request)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            response = await client.send(outgoing)
        return self._response(response, request, outgoing.headers["X-Request-ID"], gateway_id)

    @staticmethod
    def _response(response: httpx.Response, request: ResourceNavigationRequest,
                  request_id: str, gateway_id: str) -> ResourceNavigationResultDTO:
        payload = response.json()
        if response.headers.get("X-Request-ID") != request_id or payload.get("request_id") != request_id:
            raise FederationError(FEDERATION_MALFORMED_FRAME, "Gateway 文件导航响应 request_id 漂移")
        if response.is_error:
            detail = payload.get("detail")
            if isinstance(detail, dict):
                owner_detail = detail.get("detail", detail)
                if isinstance(owner_detail, dict) and owner_detail.get("owner") == "resource_file_operations":
                    raise ResourceFileOperationError(owner_detail["code"], owner_detail["message"], reason=owner_detail["reason"])
                if detail.get("owner") == "resource_uri_resolver":
                    raise ResourceUriResolveError(detail["code"], detail["message"])
                if detail.get("code") in FEDERATION_ERROR_CODES:
                    raise FederationError(detail["code"], detail["message"], detail=detail.get("detail"))
        response.raise_for_status()
        envelope = APIResponse[ResourceNavigationResultDTO].model_validate(payload)
        if envelope.code != 0 or envelope.data is None:
            raise FederationError(FEDERATION_MALFORMED_FRAME, "Gateway 文件导航响应缺少成功结果")
        if len(response.content) > request.max_response_bytes:
            raise ResourceFileOperationError("output_limit_exceeded", "Gateway 文件导航回执超过响应预算", reason="response_byte_limit")
        return qualify_navigation_result(envelope.data, uri=request.uri, gateway_id=gateway_id)
