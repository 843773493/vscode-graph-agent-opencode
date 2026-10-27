"""当前工作区持久任务的管理接口；Gateway 只转发。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.abstractions.state_conflict import ClientStateConflictError
from app.api.deps import get_persistent_task_service, get_request_id, verify_local_token
from app.api.errors import not_found_http_error, state_conflict_http_error
from app.api.sse_heartbeat import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    SSE_NO_CACHE_HEADERS,
    stream_sse_with_heartbeat,
)
from app.core.exceptions import NotFoundError
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskCheckRequest,
    PersistentTaskControlRequest,
    PersistentTaskCreateRequest,
    PersistentTaskDTO,
    PersistentTaskListDTO,
    PersistentTaskNotificationRequest,
    PersistentTaskRunDTO,
    PersistentTaskRunListDTO,
    PersistentTaskUpdateRequest,
)
from app.schemas.internal_v2.sse import sse_responses
from app.services.business.persistent_task_service import PersistentTaskService

router = APIRouter(prefix="/persistent-tasks", tags=["persistent-tasks"], dependencies=[Depends(verify_local_token)])
Result = TypeVar("Result")
TaskService = Annotated[PersistentTaskService, Depends(get_persistent_task_service)]
RequestId = Annotated[str, Depends(get_request_id)]


async def _apply(operation: Callable[[], Awaitable[Result]]) -> Result:
    try:
        return await operation()
    except ClientStateConflictError as error:
        raise state_conflict_http_error(error) from error
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("", response_model=APIResponse[PersistentTaskListDTO])
async def list_tasks(request_id: RequestId, service: TaskService):
    return APIResponse(data=PersistentTaskListDTO(items=service.list()), request_id=request_id)


@router.post("", response_model=APIResponse[PersistentTaskDTO])
async def create_task(payload: PersistentTaskCreateRequest, request_id: RequestId,
                      service: TaskService):
    return APIResponse(data=await _apply(lambda: service.create(payload)), request_id=request_id)


@router.get("/events", response_class=StreamingResponse,
            responses=sse_responses("当前工作区持久任务完整快照", {"snapshot": PersistentTaskListDTO}))
async def task_events(service: TaskService):
    channel = service.runtime.events

    async def stream():
        subscription = channel.subscribe(label="persistent-task-management")
        try:
            yield "event: snapshot\ndata: " + PersistentTaskListDTO(items=service.list()).model_dump_json() + "\n\n"
            while True:
                await subscription.next()
                # gap 直接推送权威完整列表，不能用丢失通知猜测任务状态。
                yield "event: snapshot\ndata: " + PersistentTaskListDTO(items=service.list()).model_dump_json() + "\n\n"
        finally:
            channel.unsubscribe(subscription)

    return StreamingResponse(stream_sse_with_heartbeat(stream(), heartbeat_interval_seconds=SSE_HEARTBEAT_INTERVAL_SECONDS,
                                                       close_source_on_exit=True), media_type="text/event-stream", headers=SSE_NO_CACHE_HEADERS)


@router.get("/{task_id}", response_model=APIResponse[PersistentTaskDTO])
async def get_task(task_id: str, request_id: RequestId,
                   service: TaskService):
    try:
        return APIResponse(data=service.get(task_id), request_id=request_id)
    except KeyError as error:
        raise not_found_http_error(error) from error


@router.put("/{task_id}", response_model=APIResponse[PersistentTaskDTO])
async def update_task(task_id: str, payload: PersistentTaskUpdateRequest, request_id: RequestId,
                      service: TaskService):
    return APIResponse(data=await _apply(lambda: service.update(task_id, payload)), request_id=request_id)


@router.post("/{task_id}/control", response_model=APIResponse[PersistentTaskDTO])
async def control_task(task_id: str, payload: PersistentTaskControlRequest, request_id: RequestId,
                       service: TaskService):
    return APIResponse(data=await _apply(lambda: service.control(task_id, payload)), request_id=request_id)


@router.post("/{task_id}/checks", response_model=APIResponse[PersistentTaskRunDTO])
async def check_task(task_id: str, payload: PersistentTaskCheckRequest, request_id: RequestId,
                     service: TaskService):
    return APIResponse(data=await _apply(lambda: service.check(task_id, payload)), request_id=request_id)


@router.get("/{task_id}/runs", response_model=APIResponse[PersistentTaskRunListDTO])
async def task_runs(task_id: str, request_id: RequestId,
                    service: TaskService):
    try:
        return APIResponse(data=PersistentTaskRunListDTO(items=service.runs(task_id)), request_id=request_id)
    except KeyError as error:
        raise not_found_http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/{task_id}/notifications", response_model=APIResponse[PersistentTaskRunDTO])
async def notify_task(task_id: str, payload: PersistentTaskNotificationRequest, request_id: RequestId,
                      service: TaskService):
    return APIResponse(data=await _apply(lambda: service.check(task_id, payload)), request_id=request_id)
