"""通过既有联邦 relay 传输条件文本请求；不选路重试、不代执行文件。"""
from __future__ import annotations

from collections.abc import Callable

import httpx

from app.core.resource_uri import parse_resource_uri
from app.core.trace_middleware import (
    require_current_gateway_id,
    require_current_request_id,
)
from app.gateway.federation.errors import (
    FEDERATION_ERROR_CODES,
    FEDERATION_MALFORMED_FRAME,
    FederationError,
)
from app.gateway.federation.grants import TextMutationGrantTarget
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.communication import (
    CommunicationGatewayRequest,
    TextMutationRelayRequest,
)
from app.schemas.internal_v2.text_mutation import (
    MAX_MUTATION_REQUEST_BYTES,
    TextMutationRequest,
    TextMutationTransferDTO,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.workspace_files.text_transfer import (
    qualify_text_transfer,
)


class GatewayTextMutation:
    def __init__(self, *, workspace_id: str, connection: GatewayTransportConnection,
                 local_token_supplier: Callable[[], str]) -> None:
        self._workspace_id, self._connection, self._token = workspace_id, connection, local_token_supplier

    def execute(self, request: TextMutationRequest) -> TextMutationTransferDTO:
        command = request.command
        resource = parse_resource_uri(command.target)
        if resource.gateway_authority is None:
            raise ValueError("文本传输必须固定目标 Gateway authority")
        target = TextMutationGrantTarget(gateway_id=resource.gateway_authority, uri=command.target)
        relay = TextMutationRelayRequest(source_gateway_id=require_current_gateway_id("文本修改来源"),
            source_workspace_id=self._workspace_id, mutation_target=target, mutation=request)
        payload = CommunicationGatewayRequest(operation="file_write", mutation_target=target, body=relay.model_dump(mode="json", exclude_none=True))
        content = payload.model_dump_json(exclude_none=True).encode("utf-8")
        if len(content) > MAX_MUTATION_REQUEST_BYTES:
            raise ValueError("文本修改请求超过传输预算")
        request_id = require_current_request_id("文本修改传输")
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            response = client.post(self._connection.resolve_gateway_url() + "/api/gateway/federation/relay",
                content=content, headers={"Content-Type": "application/json", "X-Local-Token": self._token(), "X-Request-ID": request_id},
                timeout=min(request.timeout_ms / 1000, self._connection.resolve_timeout_seconds()))
        if len(response.content) > request.max_response_bytes:
            raise ValueError("文本修改回执超过传输预算，目标效果尚未确认")
        data = response.json()
        if not isinstance(data, dict):
            raise FederationError(FEDERATION_MALFORMED_FRAME, "文本修改回执不是对象")
        if response.headers.get("X-Request-ID") != request_id or data.get("request_id") != request_id:
            raise FederationError(FEDERATION_MALFORMED_FRAME, "文本修改回执 request_id 漂移")
        if response.is_error:
            detail = data.get("detail")
            if isinstance(detail, dict) and detail.get("code") in FEDERATION_ERROR_CODES:
                raise FederationError(detail["code"], "目标文件调用失败，效果须在原目标核实")
        response.raise_for_status()
        envelope = APIResponse[TextMutationTransferDTO].model_validate(data)
        if envelope.code != 0 or envelope.data is None:
            raise FederationError(FEDERATION_MALFORMED_FRAME, "文本修改响应缺少目标回执")
        return qualify_text_transfer(envelope.data, command, target.gateway_id)
