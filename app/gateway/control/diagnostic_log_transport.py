"""沿原联邦管理传输访问日志 owner；查询与定位共用目标、权限和响应校验。"""

from __future__ import annotations

import asyncio
import json
from time import time
from typing import overload

import httpx
from pydantic import ValidationError

from app.core.failures import failure_detail
from app.core.path_utils import get_gateway_root
from app.core.shielded_cleanup import run_cleanup_shielded
from app.gateway.control.diagnostic_logs import (
    locate_diagnostic_log,
    query_diagnostic_logs,
)
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import send_remote_gateway_management
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.log_query import RuntimeLogQueryError
from app.schemas.gateway.diagnostics import (
    GatewayDiagnosticLogLocateRequest,
    GatewayDiagnosticLogLocationDTO,
    GatewayDiagnosticLogQueryRequest,
    GatewayDiagnosticLogQueryResultDTO,
)
from app.schemas.internal_v2.common import APIResponse

MAX_DIAGNOSTIC_RESPONSE_BYTES = 8 * 1024 * 1024


@overload
async def read_diagnostic_log_target(
    registry: GatewayWorkspaceRegistry, payload: GatewayDiagnosticLogQueryRequest, *,
    gateway_id: str, request_id: str, max_bytes: int = MAX_DIAGNOSTIC_RESPONSE_BYTES,
) -> GatewayDiagnosticLogQueryResultDTO: ...


@overload
async def read_diagnostic_log_target(
    registry: GatewayWorkspaceRegistry, payload: GatewayDiagnosticLogLocateRequest, *,
    gateway_id: str, request_id: str, max_bytes: int = MAX_DIAGNOSTIC_RESPONSE_BYTES,
) -> GatewayDiagnosticLogLocationDTO: ...


async def read_diagnostic_log_target(
    registry: GatewayWorkspaceRegistry, payload: GatewayDiagnosticLogQueryRequest | GatewayDiagnosticLogLocateRequest, *,
    gateway_id: str, request_id: str, max_bytes: int = MAX_DIAGNOSTIC_RESPONSE_BYTES,
) -> GatewayDiagnosticLogQueryResultDTO | GatewayDiagnosticLogLocationDTO:
    querying = isinstance(payload, GatewayDiagnosticLogQueryRequest)
    if gateway_id == registry.gateway_id:
        result = await query_diagnostic_logs(registry, payload, gateway_id=gateway_id) if isinstance(payload, GatewayDiagnosticLogQueryRequest) else (
            await locate_diagnostic_log(registry, payload, gateway_id=gateway_id)
        )
        if len(result.model_dump_json().encode()) > max_bytes:
            raise RuntimeLogQueryError("delivery_limit", "response_size_limit", "运行日志页超过本次字节配额，请缩小页大小后继续原分支")
        return result
    connections = [connection for connection in registry.remote_gateway_connections() if connection.remote_gateway_id == gateway_id]
    if not connections:
        raise LookupError("运行日志目标 Gateway 未登记")
    if len(connections) != 1:
        raise LookupError("运行日志目标 Gateway 的管理连接不能唯一确定，请先明确连接关系")
    connection = connections[0]
    for workspace_id in payload.scope.workspace_ids:
        target = registry.resolve((gateway_id, workspace_id))
        if target.connection_kind != "remote_gateway" or connection.connection_id not in target.remote_gateway_connection_ids:
            raise ValueError("运行日志筛选工作区不属于所选 Gateway 管理路由")
    credential = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").get(connection.connection_id)
    url = registry.remote_gateway_url(connection.connection_id)
    deadline = payload.deadline_at if isinstance(payload, GatewayDiagnosticLogQueryRequest) else None
    remaining = min(15.0, deadline - time()) if deadline is not None else 15.0
    if remaining <= 0:
        raise httpx.ReadTimeout("远端运行日志查询总期限已到，未分发请求")
    registry.acquire_remote_gateway_reference(connection.connection_id)
    response: httpx.Response | None = None

    async def close() -> None:
        try:
            if response is not None:
                await response.aclose()
        finally:
            registry.release_remote_gateway_reference(connection.connection_id)

    try:
        async with asyncio.timeout(remaining):
            response = await send_remote_gateway_management(
                gateway_url=url, credential=credential, method="POST", request_id=request_id,
                path=f"/api/gateway/federation/diagnostics/{'query' if querying else 'locate'}",
                payload=payload.model_dump(mode="json"), stream=True, timeout_seconds=remaining,
            )
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(body) + len(chunk) > max_bytes:
                    raise RuntimeLogQueryError("delivery_limit", "response_size_limit", "远端运行日志响应超过本次字节配额，请缩小页大小")
                body.extend(chunk)
            if response.headers.get("X-Request-ID") != request_id:
                raise RuntimeLogQueryError("invalid_response", "request_id_mismatch", "运行日志管理响应丢失本次 request_id")
            if response.status_code != 200:
                try:
                    payload_error = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    payload_error = None
                detail = payload_error.get("detail") if isinstance(payload_error, dict) else None
                if isinstance(detail, dict) and detail.get("owner") == "runtime_log" and all(
                    isinstance(detail.get(field), str) for field in ("code", "reason", "message")
                ):
                    raise RuntimeLogQueryError(detail["code"], detail["reason"], detail["message"], http_status=response.status_code)
                # HTTP 拒绝仍沿原传输异常发布，搜索边界保留 owner 的机器错误字段。
                rejected = httpx.Response(response.status_code, headers=response.headers, content=bytes(body), request=response.request)
                rejected.raise_for_status()
            try:
                if querying:
                    query_envelope = APIResponse[GatewayDiagnosticLogQueryResultDTO].model_validate_json(body)
                    result = query_envelope.data
                    envelope_id, envelope_code = query_envelope.request_id, query_envelope.code
                else:
                    location_envelope = APIResponse[GatewayDiagnosticLogLocationDTO].model_validate_json(body)
                    result = location_envelope.data
                    envelope_id, envelope_code = location_envelope.request_id, location_envelope.code
            except ValidationError as error:
                raise RuntimeLogQueryError("invalid_response", "schema_mismatch", failure_detail(
                    error, stage="diagnostics_response", label="远端日志响应不符合公开合同",
                )) from error
            if envelope_id != request_id or envelope_code != 0 or result is None:
                raise RuntimeLogQueryError("invalid_response", "request_id_mismatch", "运行日志响应信封与本次请求不符")
            if isinstance(result, GatewayDiagnosticLogQueryResultDTO):
                if (not isinstance(payload, GatewayDiagnosticLogQueryRequest) or result.gateway_id != gateway_id
                        or result.scope != payload.scope or result.query != payload.query or len(result.items) > payload.limit):
                    raise RuntimeLogQueryError("invalid_response", "owner_mismatch", "运行日志响应改变了目标或来源范围")
            elif (not isinstance(payload, GatewayDiagnosticLogLocateRequest) or result.reference != payload.reference
                      or result.source.source != payload.scope.source or (payload.scope.source == "workspace"
                      and not set(result.source.workspace_ids).intersection(payload.scope.workspace_ids))):
                raise RuntimeLogQueryError("invalid_response", "owner_mismatch", "运行日志定位响应改变了原记录引用")
            return result
    except TimeoutError as error:
        raise httpx.ReadTimeout("远端运行日志响应未在本次总期限内完成") from error
    finally:
        await run_cleanup_shielded(close)
