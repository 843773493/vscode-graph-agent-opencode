from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import verify_local_token
from app.services.artifact_service import ArtifactService

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}", summary="获取任务产物")
async def get_artifact(
    artifact_id: str,
    _: str = Depends(verify_local_token),
):
    return await ArtifactService.get_instance().get_response(artifact_id)
