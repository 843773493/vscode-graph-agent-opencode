from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_config_service, get_request_id, verify_local_token
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.config import (
    ConfigDTO,
    ConfigReloadStatusDTO,
    ConfigUpdateRequest,
)
from app.services.infrastructure.config_service import ConfigService

router = APIRouter(prefix="/config", tags=["config"])


def _reload_status_dto(config_service: ConfigService) -> ConfigReloadStatusDTO:
    status = config_service.get_reload_status()
    return ConfigReloadStatusDTO(
        healthy=status.healthy,
        revision=status.revision,
        restart_required=status.restart_required,
        reason=status.reason,
        changed_sections=list(status.changed_sections),
        last_success_at=status.last_success_at.isoformat(),
        last_attempt_at=status.last_attempt_at.isoformat(),
        last_error=status.last_error,
    )


@router.get("", response_model=APIResponse[ConfigDTO], summary="获取配置")
async def get_config(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    result = await config_service.get()
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/reload-status",
    response_model=APIResponse[ConfigReloadStatusDTO],
    summary="获取配置热重载状态",
)
async def get_config_reload_status(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    return APIResponse(
        data=_reload_status_dto(config_service),
        request_id=request_id,
    )


@router.patch("", response_model=APIResponse[ConfigDTO], summary="更新配置")
async def update_config(
    payload: ConfigUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    result = await config_service.update(payload)
    return APIResponse(data=result, request_id=request_id)
