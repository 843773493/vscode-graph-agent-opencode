"""进程级内部日志控制；不取得或复制任何工作区服务图。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException

from app.api import runtime_control
from app.core.logging_config import (
    RuntimeLoggingConflictError,
    apply_runtime_logging,
    runtime_logging_state,
)
from app.core.trace_middleware import get_request_id
from app.schemas.runtime_logging import RuntimeLoggingApply


def runtime_logging_router(*, prefix: str, verify_token: Callable[..., str]) -> APIRouter:
    router = APIRouter(
        prefix=prefix, include_in_schema=False,
        dependencies=[Depends(runtime_control.verify_local_control_request), Depends(verify_token)],
    )

    @router.get("")
    async def status(request_id: str = Depends(get_request_id)):
        return {"data": asdict(runtime_logging_state()), "request_id": request_id}

    @router.post("")
    async def apply(payload: RuntimeLoggingApply, request_id: str = Depends(get_request_id)):
        try:
            state = apply_runtime_logging(**payload.model_dump())
        except RuntimeLoggingConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"data": asdict(state), "request_id": request_id}

    return router
