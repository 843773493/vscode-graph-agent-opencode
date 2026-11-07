"""受认证的本机进程控制；只向原 HTTP server 请求退出。"""

from __future__ import annotations

import os
import secrets
from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request

from app.core.trace_middleware import get_request_id
from app.schemas.internal_v2.common import APIResponse
from app.schemas.runtime_control import RuntimeShutdownAccepted, RuntimeShutdownRequest


def verify_runtime_control_token(x_local_token: str | None = Header(default=None)) -> str:
    expected = os.environ.get("BOXTEAM_RUNTIME_CONTROL_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="此入口未配置本机运行时控制凭据")
    if x_local_token is None or not secrets.compare_digest(
        x_local_token.encode("utf-8"), expected.encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="invalid runtime control token")
    return x_local_token


def verify_local_control_request(request: Request) -> None:
    if (request.client is None
        or request.client.host not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        or "origin" in request.headers or "sec-fetch-site" in request.headers):
        raise HTTPException(status_code=403, detail="运行时控制只接受本机管理通道")


router = APIRouter(
    prefix="/api/internal/runtime", include_in_schema=False,
    dependencies=[Depends(verify_local_control_request), Depends(verify_runtime_control_token)],
)


@router.post("/shutdown", response_model=APIResponse[RuntimeShutdownAccepted])
async def shutdown(
    payload: RuntimeShutdownRequest, request: Request, background: BackgroundTasks,
    request_id: str = Depends(get_request_id),
) -> APIResponse[RuntimeShutdownAccepted]:
    if payload.process_id != os.getpid() or payload.backend_generation != os.environ.get("BOXTEAM_BACKEND_GENERATION"):
        raise HTTPException(status_code=409, detail="关闭请求的 Backend 进程或 generation 已变化")
    callback = cast(Callable[[], Awaitable[None]] | None,
        request.scope.get("state", {}).get("request_process_shutdown"))
    if callback is None:
        raise HTTPException(status_code=503, detail="当前 HTTP 进程未由受管 runner 启动")
    # ACK 发出后由同一事件循环请求原 Uvicorn 退出；不把已接受冒充实际退出。
    background.add_task(callback)
    return APIResponse(data=RuntimeShutdownAccepted(**payload.model_dump(), accepted=True), request_id=request_id)
