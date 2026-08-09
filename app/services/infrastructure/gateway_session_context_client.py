from __future__ import annotations

from typing import TypeVar

import httpx
from pydantic import BaseModel

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.gateway.schemas import GatewayWorkspaceListDTO
from app.schemas.public_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.services.infrastructure.config_service import ConfigService

ResponseDTO = TypeVar("ResponseDTO", bound=BaseModel)
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8014"
_MODEL_RECOVERABLE_HTTP_STATUSES = frozenset(
    {400, 401, 403, 404, 409, 422, 502, 503, 504}
)


class GatewaySessionContextClient:
    """只负责 Gateway Context HTTP 传输，不承载跨工作区合并规则。"""

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        timeout_seconds: float | None = None,
        config_service: ConfigService | None = None,
    ) -> None:
        resolved_gateway_url = (
            gateway_url
            if gateway_url is not None
            else (
                config_service.get_gateway_connection_url()
                if config_service is not None
                else DEFAULT_GATEWAY_URL
            )
        )
        resolved_timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else (
                config_service.get_gateway_connection_timeout_seconds()
                if config_service is not None
                else 30
            )
        )
        self._gateway_url = resolved_gateway_url.rstrip("/")
        self._timeout_seconds = resolved_timeout_seconds

    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO:
        return await self._request(
            "GET",
            "/api/gateway/workspaces",
            response_type=GatewayWorkspaceListDTO,
        )

    async def read_context_in_workspace(
        self,
        workspace_id: str,
        request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        return await self._request(
            "POST",
            "/api/v1/context/read",
            workspace_id=workspace_id,
            response_type=SessionContextReadResultDTO,
            json_body=request.model_dump(mode="json"),
        )

    async def search_context_in_workspace(
        self,
        workspace_id: str,
        request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO:
        return await self._request(
            "POST",
            "/api/v1/context/search",
            workspace_id=workspace_id,
            response_type=SessionContextSearchResultDTO,
            json_body=request.model_dump(mode="json"),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        response_type: type[ResponseDTO],
        workspace_id: str | None = None,
        json_body: dict[str, object] | None = None,
    ) -> ResponseDTO:
        if workspace_id is not None and not workspace_id.strip():
            raise WorkspaceSessionContextAccessError(
                "workspace_id 不能为空；请使用 Gateway inventory 返回的工作区 ID"
            )
        headers = (
            {"X-BoxTeam-Workspace-Id": workspace_id.strip()}
            if workspace_id is not None
            else None
        )
        async with httpx.AsyncClient(
            base_url=self._gateway_url,
            timeout=self._timeout_seconds,
            headers=headers,
        ) as client:
            try:
                response = await client.request(method, path, json=json_body)
            except httpx.RequestError as error:
                raise WorkspaceSessionContextAccessError(
                    "无法连接 Workspace Gateway: "
                    f"workspace_id={workspace_id}, method={method}, path={path}, "
                    f"error_type={type(error).__name__}, error={error}"
                ) from error
        if not response.is_success:
            message = (
                "Gateway 上下文查询失败: "
                f"workspace_id={workspace_id}, method={method}, path={path}, "
                f"status={response.status_code}, detail={response.text[:2000]}"
            )
            if response.status_code in _MODEL_RECOVERABLE_HTTP_STATUSES:
                raise WorkspaceSessionContextAccessError(message)
            raise RuntimeError(message)
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise RuntimeError(f"Gateway 上下文查询响应缺少 data object: path={path}")
        return response_type.model_validate(payload["data"])
