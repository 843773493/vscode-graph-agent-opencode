from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.abstractions.job_service import JobServiceProtocol
from app.api.deps import (
    get_artifact_service,
    get_event_service,
    get_job_service,
    get_request_id,
    verify_local_token,
)
from app.api.errors import unimplemented_http_error
from app.schemas.event import Event
from app.schemas.internal_v2.artifact import ArtifactDTO
from app.schemas.internal_v2.common import APIResponse, ControlAction
from app.schemas.internal_v2.job import (
    JobControlRequest,
    JobControlResponseDTO,
    JobDTO,
    StepDTO,
)
from app.schemas.internal_v2.session_interaction import SessionExecutionSseDTO
from app.schemas.internal_v2.sse import sse_responses
from app.services.event_service import EventService, JobEventCursorGoneError
from app.services.infrastructure.artifact_service import ArtifactService

router = APIRouter(prefix="/jobs", tags=["jobs"])

# JobControlService 当前只实现了这三个动作，其余动作由它显式拒绝为「尚未实现」。
UNIMPLEMENTED_CONTROL_ACTIONS = frozenset(
    set(ControlAction)
    - {ControlAction.pause, ControlAction.resume, ControlAction.cancel}
)


def _job_control_http_error(
    job_id: str,
    payload: JobControlRequest,
    error: ValueError,
) -> HTTPException:
    """把 Job 控制被拒落成对客户端有意义的响应，而不是无上下文 500。

    ``JobService.control`` 与 ``JobControlService`` 已用同一个类型名和同一句
    「Job {id} not found」表达未知 Job；本适配层对 ``get_job``/``list_job_steps``
    也按同一句文本落 404，这里保持一致。

    TODO: ``JobControlValueError`` 目前把「未知 Job」「动作未实现」「状态不允许」
    压在同一类型上，只能靠文本区分；业务层暴露独立错误身份后应改为按类型判定。
    """
    if error.args == (f"Job {job_id} not found",):
        return HTTPException(status_code=404, detail=str(error))
    if payload.action in UNIMPLEMENTED_CONTROL_ACTIONS:
        return unimplemented_http_error(error)
    # 状态不允许该动作，或会话已有其他 active Job：状态冲突而非服务端故障。
    return HTTPException(status_code=409, detail=str(error))


@router.get("/{job_id}", response_model=APIResponse[JobDTO], summary="获取任务详情")
async def get_job(
    job_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    try:
        result = await job_service.get(job_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{job_id}/steps", response_model=APIResponse[list[StepDTO]], summary="获取任务步骤"
)
async def list_job_steps(
    job_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    try:
        result = await job_service.list_steps(job_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{job_id}/events", response_model=APIResponse[list[Event]], summary="获取任务事件"
)
async def list_job_events(
    job_id: str,
    after: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    event_service: EventService = Depends(get_event_service),
):
    result = await event_service.list(job_id=job_id, after=after, limit=limit)
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{job_id}/events/stream",
    response_class=StreamingResponse,
    summary="订阅任务事件流",
    responses=sse_responses(
        "SSE Job 观察事件流",
        {"*": SessionExecutionSseDTO},
    ),
)
async def stream_job_events(
    job_id: str,
    request: Request,
    after_event_id: str | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    event_service: EventService = Depends(get_event_service),
):
    if after_event_id and last_event_id and after_event_id != last_event_id:
        raise HTTPException(
            status_code=409,
            detail="after_event_id 与 Last-Event-ID 不一致",
        )
    cursor = last_event_id or after_event_id
    try:
        await event_service.ensure_cursor(job_id, cursor)
    except JobEventCursorGoneError as error:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "job_event_cursor_gone",
                "message": str(error),
                "job_id": job_id,
                "event_id": error.event_id,
            },
        ) from error
    subscriber_metadata = {
        "request_id": request_id,
        "client_host": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
    }

    async def event_generator():
        async for chunk in event_service.stream_sse(
            job_id,
            after_event_id=cursor,
            subscriber_metadata=subscriber_metadata,
        ):
            yield chunk

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post(
    "/{job_id}/control",
    response_model=APIResponse[JobControlResponseDTO],
    summary="控制任务",
)
async def control_job(
    job_id: str,
    payload: JobControlRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    try:
        result = await job_service.control(job_id, payload)
    except ValueError as error:
        raise _job_control_http_error(job_id, payload, error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{job_id}/artifacts",
    response_model=APIResponse[list[ArtifactDTO]],
    summary="获取任务产物列表",
)
async def list_job_artifacts(
    job_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    artifact_service: ArtifactService = Depends(get_artifact_service),
):
    try:
        result = await artifact_service.list_by_job(job_id)
    except RuntimeError as error:
        # 产物存储尚未实现：服务端能力缺失，显式落 501 而不是无上下文的 500。
        raise unimplemented_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)
