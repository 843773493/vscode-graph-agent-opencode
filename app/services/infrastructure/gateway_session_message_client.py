from __future__ import annotations

from typing import TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.gateway.auth import get_gateway_local_token
from app.schemas.public_v2.message import (
    MessageRunAccepted,
    SessionMessageDispatchRequest,
)
from app.schemas.public_v2.pending_request import DeliveryPolicy
from app.services.infrastructure.config_service import ConfigService

ResponseDTO = TypeVar("ResponseDTO", bound=BaseModel)
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8014"
_MODEL_RECOVERABLE_HTTP_STATUSES = frozenset(
    {400, 401, 403, 404, 409, 422, 502, 503, 504}
)


class GatewaySessionMessageClient:
    """只负责通过 Gateway 代理向远端工作区派发会话消息。"""

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        timeout_seconds: float | None = None,
        config_service: ConfigService | None = None,
    ) -> None:
        self._gateway_url = (
            gateway_url
            if gateway_url is not None
            else (
                config_service.get_gateway_connection_url()
                if config_service is not None
                else DEFAULT_GATEWAY_URL
            )
        ).rstrip("/")
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else (
                config_service.get_gateway_connection_timeout_seconds()
                if config_service is not None
                else 30
            )
        )

    async def dispatch(
        self,
        session_id: str,
        *,
        workspace_id: str,
        content: str,
        metadata: dict[str, object],
        simulate_user: bool,
        delivery_policy: DeliveryPolicy,
        idempotency_key: str | None,
    ) -> MessageRunAccepted:
        payload = SessionMessageDispatchRequest(
            content=content,
            metadata=metadata,
            simulate_user=simulate_user,
            delivery_policy=delivery_policy,
            idempotency_key=idempotency_key,
        )
        return await self._request(
            f"/api/v1/sessions/{quote(session_id, safe='')}/inter-agent-messages",
            workspace_id=workspace_id,
            json_body=payload.model_dump(mode="json"),
        )

    async def _request(
        self,
        path: str,
        *,
        workspace_id: str,
        json_body: dict[str, object],
    ) -> MessageRunAccepted:
        async with httpx.AsyncClient(
            base_url=self._gateway_url,
            timeout=self._timeout_seconds,
            headers={
                "X-Local-Token": get_gateway_local_token(),
                "X-BoxTeam-Workspace-Id": workspace_id,
            },
        ) as client:
            try:
                response = await client.post(
                    path,
                    json=json_body,
                )
            except httpx.RequestError as error:
                raise WorkspaceSessionContextAccessError(
                    "无法连接 Workspace Gateway 发送会话消息: "
                    f"path={path}, error_type={type(error).__name__}, error={error}"
                ) from error
        if not response.is_success:
            message = (
                "Gateway 会话消息派发失败: "
                f"path={path}, status={response.status_code}, "
                f"detail={response.text[:2000]}"
            )
            if response.status_code in _MODEL_RECOVERABLE_HTTP_STATUSES:
                raise WorkspaceSessionContextAccessError(message)
            raise RuntimeError(message)
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise TypeError(
                f"Gateway 会话消息派发响应缺少 data object: path={path}"
            )
        return MessageRunAccepted.model_validate(payload["data"])


__all__ = ["GatewaySessionMessageClient"]
