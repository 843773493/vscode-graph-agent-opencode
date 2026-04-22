from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.common import APIResponse
from app.services.runtime_service import RuntimeService

router = APIRouter(prefix="/runtime", tags=["runtime"])


@router.get("/status", response_model=APIResponse[dict], summary="获取运行时状态")
async def get_runtime_status(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await RuntimeService.get_instance().status()
    return APIResponse(data=result, request_id=request_id)


@router.post("/shutdown", response_model=APIResponse[dict], summary="关闭运行时")
async def shutdown_runtime(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await RuntimeService.get_instance().shutdown()
    return APIResponse(data=result, request_id=request_id)
