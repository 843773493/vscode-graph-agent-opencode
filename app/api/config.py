from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_config_service, get_request_id, verify_local_token
from app.schemas.common import APIResponse
from app.schemas.config import ConfigDTO, ConfigUpdateRequest
from app.services.config_service import ConfigService

router = APIRouter(prefix="/config", tags=["config"])


@router.get("", response_model=APIResponse[ConfigDTO], summary="获取配置")
async def get_config(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    result = await config_service.get()
    return APIResponse(data=result, request_id=request_id)


@router.patch("", response_model=APIResponse[ConfigDTO], summary="更新配置")
async def update_config(
    payload: ConfigUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    result = await config_service.update(payload)
    return APIResponse(data=result, request_id=request_id)
