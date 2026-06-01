from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import get_artifact_service, get_event_service, get_job_service, get_request_id, verify_local_token
from app.schemas.artifact import ArtifactDTO
from app.schemas.common import APIResponse
from app.schemas.event import Event as SSEEvent
from app.schemas.job import JobControlRequest, JobControlResponseDTO, JobDTO, StepDTO
from app.services.artifact_service import ArtifactService
from app.services.event_service import EventService
from app.services.job_service import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=APIResponse[JobDTO], summary="获取任务详情")
async def get_job(
    job_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    job_service: JobService = Depends(get_job_service),
):
    result = await job_service.get(job_id)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{job_id}/steps", response_model=APIResponse[list[StepDTO]], summary="获取任务步骤")
async def list_job_steps(
    job_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    job_service: JobService = Depends(get_job_service),
):
    result = await job_service.list_steps(job_id)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{job_id}/events", response_model=APIResponse[list[SSEEvent]], summary="获取任务事件")
async def list_job_events(
    job_id: str,
    after: str | None = None,
    limit: int = 100,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    event_service: EventService = Depends(get_event_service),
):
    result = await event_service.list(job_id=job_id, after=after, limit=limit)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{job_id}/events/stream", summary="订阅任务事件流")
async def stream_job_events(
    job_id: str,
    _: str = Depends(verify_local_token),
    event_service: EventService = Depends(get_event_service),
):
    async def event_generator():
        async for chunk in event_service.stream_sse(job_id):
            yield chunk

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/{job_id}/control", response_model=APIResponse[JobControlResponseDTO], summary="控制任务")
async def control_job(
    job_id: str,
    payload: JobControlRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    job_service: JobService = Depends(get_job_service),
):
    result = await job_service.control(job_id, payload)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{job_id}/artifacts", response_model=APIResponse[list[ArtifactDTO]], summary="获取任务产物列表")
async def list_job_artifacts(
    job_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    artifact_service: ArtifactService = Depends(get_artifact_service),
):
    result = await artifact_service.list_by_job(job_id)
    return APIResponse(data=result, request_id=request_id)
