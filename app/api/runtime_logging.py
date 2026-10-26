"""进程级内部日志控制；不取得或复制任何工作区服务图。"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from dataclasses import asdict

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.core.logging_config import (
    RuntimeLoggingConflictError,
    apply_runtime_logging,
    runtime_logging_state,
)
from app.core.trace_middleware import get_request_id
from app.schemas.runtime_logging import RuntimeLoggingApply


def verify_runtime_control_token(x_local_token: str | None = Header(default=None)) -> str:
    expected = os.environ.get("BOXTEAM_RUNTIME_CONTROL_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="此入口未配置本机运行时控制凭据")
    if x_local_token is None or not secrets.compare_digest(
        x_local_token.encode("utf-8"), expected.encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="invalid runtime control token")
    return x_local_token


def _verify_local_control_request(request: Request) -> None:
    if (request.client is None
        or request.client.host not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        or "origin" in request.headers or "sec-fetch-site" in request.headers):
        raise HTTPException(status_code=403, detail="运行时日志控制只接受本机管理通道")


def runtime_logging_router(*, prefix: str, verify_token: Callable[..., str]) -> APIRouter:
    router = APIRouter(
        prefix=prefix, include_in_schema=False,
        dependencies=[Depends(_verify_local_control_request), Depends(verify_token)],
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
