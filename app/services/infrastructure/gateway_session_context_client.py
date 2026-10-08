from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import httpx
from pydantic import BaseModel

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.core.trace_middleware import (
    require_current_gateway_id,
    require_current_request_id,
)
from app.gateway.federation.grants import ContextGrantTarget
from app.schemas.gateway.workspaces import GatewayWorkspaceListDTO
from app.schemas.internal_v2.communication import ContextQueryRequest
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.services.business.session_context_resource import parse_session_context_ref
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.gateway_client_common import (
    MODEL_RECOVERABLE_HTTP_STATUSES,
    GatewayTransportConnection,
)

ResponseDTO = TypeVar("ResponseDTO", bound=BaseModel)


class GatewaySessionContextClient:
    """只负责 Gateway Context HTTP 传输，不承载跨工作区合并规则。"""

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        timeout_seconds: float | None = None,
        config_service: ConfigService | None = None,
        local_token_supplier: Callable[[], str],
    ) -> None:
        self._connection = GatewayTransportConnection(
            gateway_url=gateway_url,
            timeout_seconds=timeout_seconds,
            config_service=config_service,
        )
        self._local_token_supplier = local_token_supplier

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

    async def read_resource_context(
        self, request: SessionContextReadRequest,
    ) -> SessionContextReadResultDTO:
        return await self._resource_query(request, SessionContextReadResultDTO)

    async def search_resource_context(
        self, request: SessionContextSearchRequest,
    ) -> SessionContextSearchResultDTO:
        return await self._resource_query(request, SessionContextSearchResultDTO)

    async def _resource_query(
        self, request: SessionContextReadRequest | SessionContextSearchRequest,
        response_type: type[ResponseDTO],
    ) -> ResponseDTO:
        source_gateway_id = require_current_gateway_id("上下文资源 relay")
        resource = parse_session_context_ref(
            resource_identity=request.resource_identity, uri=request.uri, scope=request.scope,
            local_gateway_id=source_gateway_id,
        )
        target = ContextGrantTarget(
            gateway_id=resource.gateway_authority or source_gateway_id,
            resource_identity=str(resource.resource_identity), uri=resource.uri, scope=resource.scope,
        )
        request = request.model_copy(update={"uri":resource.uri})
        query = ContextQueryRequest(
            source_gateway_id=source_gateway_id, resource_target=target,
            read=request if isinstance(request, SessionContextReadRequest) else None,
            search=request if isinstance(request, SessionContextSearchRequest) else None,
        )
        return await self._request(
            "POST", "/api/gateway/federation/relay", response_type=response_type,
            json_body={"operation":"read", "resource_target":target.to_payload(), "body":query.model_dump(mode="json")},
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
        headers = {"X-Local-Token": self._local_token_supplier()}
        request_id = None
        if path == "/api/gateway/federation/relay":
            request_id = require_current_request_id("上下文资源 relay")
            headers["X-Request-ID"] = request_id
        if workspace_id is not None:
            headers["X-BoxTeam-Workspace-Id"] = workspace_id.strip()
        async with httpx.AsyncClient(
            base_url=self._connection.resolve_gateway_url(),
            timeout=self._connection.resolve_timeout_seconds(),
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
            if response.status_code in MODEL_RECOVERABLE_HTTP_STATUSES:
                raise WorkspaceSessionContextAccessError(message)
            raise RuntimeError(message)
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise TypeError(f"Gateway 上下文查询响应缺少 data object: path={path}")
        if request_id is not None and (payload.get("request_id") != request_id or response.headers.get("X-Request-ID") != request_id):
            raise RuntimeError("上下文资源 relay 响应 request_id 漂移")
        return response_type.model_validate(payload["data"])
