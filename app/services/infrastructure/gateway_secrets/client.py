"""每个新执行边界批量解析所需密钥，不在单次模型调用中轮询。"""

from collections.abc import Iterable

import httpx
from boxteam_plugin_sdk.request_context import require_current_request_id

from app.core.trace_middleware import require_current_gateway_id
from app.gateway.auth import get_gateway_local_token
from app.schemas.gateway.secrets import ResolvedSecret, ResolveSecretsRequest
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection


class GatewaySecretsClient:
    def __init__(self, connection: GatewayTransportConnection) -> None:
        self._connection = connection

    async def resolve(self, ids: Iterable[str]) -> dict[str, ResolvedSecret]:
        selected = list(dict.fromkeys(ids))
        if not selected:
            return {}
        payload = ResolveSecretsRequest(ids=selected)
        request_id = require_current_request_id("命名密钥解析")
        gateway_id = require_current_gateway_id("命名密钥解析")
        async with httpx.AsyncClient(trust_env=False, timeout=self._connection.resolve_timeout_seconds()) as client:
            response = await client.post(self._connection.resolve_gateway_url() + "/api/gateway/internal/secrets/resolve",
                json=payload.model_dump(), headers={"X-Local-Token": get_gateway_local_token(), "X-Request-ID": request_id})
        if not response.is_success:
            # 不拼接响应正文、请求对象或密钥值进入异常；缺失引用保留所请求的 ID。
            raise RuntimeError(f"命名密钥解析失败: status={response.status_code}, ids={','.join(selected)}, request_id={request_id}")
        body = response.json()
        if response.headers.get("X-Request-ID") != request_id or body.get("request_id") != request_id:
            raise RuntimeError("命名密钥解析回执的 request_id 不匹配")
        data = body["data"]
        if data["gateway_id"] != gateway_id:
            raise RuntimeError("命名密钥解析回执不属于当前 Gateway")
        if set(data["items"]) != set(selected):
            raise RuntimeError("命名密钥解析回执与请求引用不匹配")
        return {key: ResolvedSecret.model_validate(value) for key, value in data["items"].items()}
