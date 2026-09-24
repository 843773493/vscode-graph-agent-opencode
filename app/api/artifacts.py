from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_artifact_service, get_request_id, verify_local_token
from app.api.errors import unimplemented_http_error
from app.schemas.internal_v2.artifact import ArtifactDTO
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.artifact_service import ArtifactService

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get(
    "/{artifact_id}",
    response_model=APIResponse[ArtifactDTO],
    summary="获取任务产物",
)
async def get_artifact(
    artifact_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    artifact_service: ArtifactService = Depends(get_artifact_service),
):
    try:
        result = await artifact_service.get_response(artifact_id)
    except RuntimeError as error:
        # 产物存储尚未实现：服务端能力缺失，显式落 501 而不是无上下文的 500。
        raise unimplemented_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)
