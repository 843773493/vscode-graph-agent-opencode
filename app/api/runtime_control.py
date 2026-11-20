"""受认证的本机进程控制；只向原 HTTP server 请求退出。"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import cast

from boxteam_plugin_sdk.http import (
    get_request_id,
    verify_local_control_request,
    verify_runtime_control_token,
)
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from app.schemas.internal_v2.common import APIResponse
from app.schemas.runtime_control import RuntimeShutdownAccepted, RuntimeShutdownRequest

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
