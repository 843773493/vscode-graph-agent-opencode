"""原 Gateway 日志管理的具名正文查询与原行定位入口。"""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.core.failures import failure_detail
from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_federation_token, verify_gateway_token
from app.gateway.control.diagnostic_log_transport import read_diagnostic_log_target
from app.gateway.control.diagnostic_logs import (
    locate_diagnostic_log,
    query_diagnostic_logs,
)
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER, parse_target_gateway_id
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import get_registry
from app.gateway.runtime.log_query import RuntimeLogQueryError
from app.schemas.gateway.diagnostics import (
    GatewayDiagnosticLogLocateRequest,
    GatewayDiagnosticLogLocationDTO,
    GatewayDiagnosticLogQueryRequest,
    GatewayDiagnosticLogQueryResultDTO,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter(tags=["gateway-diagnostics"])


def diagnostic_log_http_error(error: RuntimeLogQueryError) -> HTTPException:
    status = 400 if error.code.startswith("invalid_runtime_log") else 409
    if error.code == "delivery_limit":
        status = 413
    elif error.code == "invalid_response":
        status = 502
    if error.http_status is not None:
        status = error.http_status
    return HTTPException(status, detail={"owner": "runtime_log", "code": error.code, "reason": error.reason, "message": str(error)})


@router.post("/api/gateway/diagnostics/query", response_model=APIResponse[GatewayDiagnosticLogQueryResultDTO])
async def query_logs(
    payload: GatewayDiagnosticLogQueryRequest, request: Request,
    _: Annotated[str, Depends(verify_gateway_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
    target_gateway_id: Annotated[str | None, Header(alias=TARGET_GATEWAY_ID_HEADER)] = None,
):
    try:
        result = await read_diagnostic_log_target(registry, payload, gateway_id=parse_target_gateway_id(target_gateway_id) or request.app.state.gateway_id,
                                                request_id=request_id)
    except RuntimeLogQueryError as error:
        raise diagnostic_log_http_error(error) from error
    except LookupError as error:
        raise HTTPException(404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from error
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        raise HTTPException(502, detail=failure_detail(error, stage="diagnostics_request", label="目标 Gateway 日志查询失败")) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/api/gateway/diagnostics/locate", response_model=APIResponse[GatewayDiagnosticLogLocationDTO])
async def locate_log(
    payload: GatewayDiagnosticLogLocateRequest,
    _: Annotated[str, Depends(verify_gateway_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
):
    try:
        result = await read_diagnostic_log_target(registry, payload, gateway_id=payload.reference.gateway_id, request_id=request_id)
    except RuntimeLogQueryError as error:
        raise diagnostic_log_http_error(error) from error
    except LookupError as error:
        raise HTTPException(404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from error
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        raise HTTPException(502, detail=failure_detail(error, stage="diagnostics_request", label="目标 Gateway 日志定位失败")) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/api/gateway/federation/diagnostics/query", response_model=APIResponse[GatewayDiagnosticLogQueryResultDTO])
async def federation_query_logs(
    payload: GatewayDiagnosticLogQueryRequest, request: Request,
    _: Annotated[object, Depends(verify_federation_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
):
    try:
        result = await query_diagnostic_logs(registry, payload, gateway_id=request.app.state.gateway_id)
    except RuntimeLogQueryError as error:
        raise diagnostic_log_http_error(error) from error
    except LookupError as error:
        raise HTTPException(404, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/api/gateway/federation/diagnostics/locate", response_model=APIResponse[GatewayDiagnosticLogLocationDTO])
async def federation_locate_log(
    payload: GatewayDiagnosticLogLocateRequest, request: Request,
    _: Annotated[object, Depends(verify_federation_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
):
    try:
        result = await locate_diagnostic_log(registry, payload, gateway_id=request.app.state.gateway_id)
    except RuntimeLogQueryError as error:
        raise diagnostic_log_http_error(error) from error
    except LookupError as error:
        raise HTTPException(404, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)
