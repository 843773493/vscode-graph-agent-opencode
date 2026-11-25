"""工作区派生执行的受理、有限观察与取消入口。"""

from __future__ import annotations

from typing import Annotated

from boxteam_plugin_sdk.http import get_request_id
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.routing import APIRoute

from app.api.deps import get_derived_execution_service, verify_local_token
from app.api.errors import client_error_message
from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.derived_execution import (
    DerivedExecutionDTO,
    DerivedExecutionScopeRequest,
    DerivedExecutionSubmitRequest,
)
from app.services.business.derived_execution_service import (
    DerivedExecutionBusyError,
    DerivedExecutionConflictError,
    DerivedExecutionService,
)


class DerivedExecutionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded_request(request: Request):
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 512 * 1024:
                    raise HTTPException(status_code=413, detail="派生请求不得超过 512 KiB")
                chunks.append(chunk)
            # 在 JSON 解析前限额，随后让 FastAPI 沿原校验及错误序列化流程消费。
            request._body = b"".join(chunks)
            return await handler(request)

        return bounded_request


router = APIRouter(prefix="/derived-executions", tags=["derived-executions"], route_class=DerivedExecutionRoute)


@router.post("", response_model=APIResponse[DerivedExecutionDTO])
async def submit_derived_execution(
    payload: DerivedExecutionSubmitRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[DerivedExecutionService, Depends(get_derived_execution_service)],
):
    try:
        result = await service.submit(payload)
    except DerivedExecutionConflictError as error:
        raise HTTPException(status_code=409, detail=client_error_message(error)) from error
    except DerivedExecutionBusyError as error:
        raise HTTPException(status_code=429, detail=client_error_message(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=client_error_message(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get("/{task_id}", response_model=APIResponse[DerivedExecutionDTO])
async def get_derived_execution(
    task_id: str,
    session_id: CanonicalSessionId,
    thread_id: CanonicalThreadId,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[DerivedExecutionService, Depends(get_derived_execution_service)],
    wait_seconds: Annotated[float, Query(ge=0, le=30, allow_inf_nan=False)] = 0,
):
    try:
        result = await service.get(session_id, thread_id, task_id, wait_seconds=wait_seconds)
    except DerivedExecutionConflictError as error:
        raise HTTPException(status_code=409, detail=client_error_message(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=client_error_message(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/{task_id}/cancel", response_model=APIResponse[DerivedExecutionDTO])
async def cancel_derived_execution(
    task_id: str,
    payload: DerivedExecutionScopeRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[DerivedExecutionService, Depends(get_derived_execution_service)],
):
    try:
        result = await service.cancel(payload.session_id, payload.thread_id, task_id)
    except DerivedExecutionConflictError as error:
        raise HTTPException(status_code=409, detail=client_error_message(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=client_error_message(error)) from error
    return APIResponse(data=result, request_id=request_id)
