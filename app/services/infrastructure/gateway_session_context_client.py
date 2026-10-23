from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

import httpx
from pydantic import BaseModel

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.core.trace_middleware import (
    require_current_gateway_id,
    require_current_request_id,
)
from app.gateway.federation.errors import FEDERATION_TARGET_OWNER_REJECTED
from app.gateway.federation.grants import ContextGrantTarget
from app.schemas.gateway.workspaces import GatewayWorkspaceListDTO
from app.schemas.internal_v2.communication import ContextQueryRequest
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.schemas.internal_v2.session_fork_transfer import (
    ForkSourceCaptureDTO,
    SessionForkTransferRequest,
    SessionForkTransferResultDTO,
)
from app.services.business.session_context_resource import parse_session_context_ref
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.gateway_client_common import (
    MODEL_RECOVERABLE_HTTP_STATUSES,
    GatewayTransportConnection,
)
from app.services.infrastructure.rollout_context.fork.source_capture import (
    IncompleteForkSourceCaptureError,
)
from app.services.infrastructure.rollout_context.fork.transfer import (
    verify_capture_identity,
)

ResponseDTO = TypeVar("ResponseDTO", bound=BaseModel)
logger = logging.getLogger(__name__)


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
        self._http_client = httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._http_client.aclose()

    async def list_gateway_workspaces(self) -> GatewayWorkspaceListDTO:
        return await self._request(
            "GET",
            "/api/gateway/workspaces",
            response_type=GatewayWorkspaceListDTO,
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

    async def fork_transfer(self, request: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
        """只传一个有界 artifact 分块；正文冻结及目标发布归两端 owner。"""
        if request.source_gateway_id != require_current_gateway_id("fork relay"):
            raise ValueError("fork transport origin 与当前 Gateway 不一致")
        path = "/api/gateway/federation/relay"
        response, request_id = await self._send_request(
            "POST", path,
            json_body={"operation": "fork_" + request.fork_target.fork_role,
                       "fork_target": request.fork_target.to_payload(), "body": request.model_dump(mode="json")},
        )
        if not response.is_success:
            self._raise_incomplete_capture(response, request=request, request_id=request_id)
            self._raise_http_error(response, method="POST", path=path)
        try:
            reply = self._decode_response(response, path=path, request_id=request_id, response_type=SessionForkTransferResultDTO)
            return reply.validate_for_request(request)
        except (KeyError, TypeError, ValueError) as error:
            detail = (
                f"fork_response_protocol_invalid: Gateway fork 回执协议无效: "
                f"request_id={request_id}, uri={request.fork_target.uri}, action={request.action}, "
                f"stage=response_validation, error_type={type(error).__name__}"
            )
            logger.error("%s；完整诊断见异常链", detail, exc_info=error)
            raise RuntimeError(detail) from error

    async def _request(
        self,
        method: str,
        path: str,
        *,
        response_type: type[ResponseDTO],
        json_body: dict[str, object] | None = None,
    ) -> ResponseDTO:
        response, request_id = await self._send_request(method, path, json_body=json_body)
        self._raise_http_error(response, method=method, path=path)
        try:
            return self._decode_response(response, path=path, request_id=request_id, response_type=response_type)
        except (KeyError, TypeError, ValueError) as error:
            detail = (
                "Gateway 上下文查询回执协议无效: "
                f"method={method}, path={path}, request_id={request_id}, "
                f"stage=response_validation, error_type={type(error).__name__}"
            )
            logger.error("%s；完整诊断见异常链", detail, exc_info=error)
            raise RuntimeError(detail) from error

    @staticmethod
    def _raise_incomplete_capture(
        response: httpx.Response, *, request: SessionForkTransferRequest, request_id: str | None,
    ) -> None:
        if response.status_code != 409:
            return
        try:
            payload = response.json()
        except ValueError:
            return
        federation = payload.get("detail") if isinstance(payload, dict) else None
        detail = federation.get("detail") if isinstance(federation, dict) else None
        if not isinstance(federation, dict) or not isinstance(detail, dict) or detail.get("code") != IncompleteForkSourceCaptureError.code:
            return
        try:
            if federation["code"] != FEDERATION_TARGET_OWNER_REJECTED or detail["owner"] != IncompleteForkSourceCaptureError.owner:
                raise ValueError("fork incomplete 回执缺少原 source owner 错误身份")
            if request_id is None or payload.get("request_id") != request_id or response.headers.get("X-Request-ID") != request_id:
                raise ValueError("fork incomplete 回执 request_id 漂移")
            if request.action != "capture" or detail["action"] != request.action or request.capture is None:
                raise ValueError("fork incomplete 回执不属于本 capture 请求")
            received = ForkSourceCaptureDTO.model_validate(detail["capture"])
            capture = verify_capture_identity(request.capture.to_record(), received)
            if request.capture.state != "preparing" or capture.state != "preparing" or capture.snapshot_sha256 is not None or capture.marker_sha256 is not None or capture.abort_reason is not None:
                raise ValueError("fork incomplete 回执不是未发布 marker 的 preparing capture")
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            detail = (
                f"fork_response_protocol_invalid: Gateway fork 错误回执协议无效: "
                f"request_id={request_id}, uri={request.fork_target.uri}, action={request.action}, "
                f"stage=incomplete_capture_validation, error_type={type(error).__name__}"
            )
            logger.error("%s；完整诊断见异常链", detail, exc_info=error)
            raise RuntimeError(detail) from error
        raise IncompleteForkSourceCaptureError(capture)

    async def _send_request(
        self, method: str, path: str, *,
        json_body: dict[str, object] | None = None,
    ) -> tuple[httpx.Response, str | None]:
        headers = {"X-Local-Token": self._local_token_supplier()}
        request_id = None
        if path == "/api/gateway/federation/relay":
            request_id = require_current_request_id("上下文资源 relay")
            headers["X-Request-ID"] = request_id
        try:
            response = await self._http_client.request(
                method, self._connection.resolve_gateway_url() + path,
                json=json_body, headers=headers,
                timeout=self._connection.resolve_timeout_seconds(),
            )
        except httpx.RequestError as error:
            raise WorkspaceSessionContextAccessError(
                "无法连接 Workspace Gateway: "
                f"method={method}, path={path}, "
                f"error_type={type(error).__name__}, error={error}"
            ) from error
        return response, request_id

    @staticmethod
    def _raise_http_error(
        response: httpx.Response, *, method: str, path: str,
    ) -> None:
        if not response.is_success:
            message = (
                "Gateway 上下文查询失败: "
                f"method={method}, path={path}, "
                f"status={response.status_code}, detail={response.text[:2000]}"
            )
            if response.status_code in MODEL_RECOVERABLE_HTTP_STATUSES:
                raise WorkspaceSessionContextAccessError(message)
            raise RuntimeError(message)

    @staticmethod
    def _decode_response(
        response: httpx.Response, *, path: str, request_id: str | None, response_type: type[ResponseDTO],
    ) -> ResponseDTO:
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise TypeError(f"Gateway 上下文查询响应缺少 data object: path={path}")
        if request_id is not None and (payload.get("request_id") != request_id or response.headers.get("X-Request-ID") != request_id):
            raise RuntimeError("上下文资源 relay 响应 request_id 漂移")
        return response_type.model_validate(payload["data"])
