"""当前工作区持久任务的管理接口；Gateway 只转发。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from app.abstractions.state_conflict import ClientStateConflictError
from app.api.deps import get_persistent_task_service, get_request_id, verify_local_token
from app.api.errors import (
    client_error_message,
    not_found_http_error,
    state_conflict_http_error,
)
from app.api.sse_heartbeat import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    SSE_NO_CACHE_HEADERS,
    stream_sse_with_heartbeat,
)
from app.core.exceptions import NotFoundError
from app.core.shielded_cleanup import run_sync_shielded
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.generator_import import SessionGeneratorImportRequest
from app.schemas.internal_v2.persistent_task import (
    PersistentTaskChangedDTO,
    PersistentTaskCheckRequest,
    PersistentTaskControlRequest,
    PersistentTaskCreateRequest,
    PersistentTaskDTO,
    PersistentTaskListDTO,
    PersistentTaskNotificationRequest,
    PersistentTaskRunDetailDTO,
    PersistentTaskRunDTO,
    PersistentTaskRunListDTO,
    PersistentTaskUpdateRequest,
)
from app.schemas.internal_v2.sse import SseErrorDTO, sse_responses
from app.services.business.persistent_task_service import PersistentTaskService
from app.services.orchestration.persistent_tasks.generator_import import (
    import_generator,
)

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
    except (ValidationError, PydanticSerializationError):
        raise
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("", response_model=APIResponse[PersistentTaskListDTO])
def list_tasks(request_id: RequestId, service: TaskService, session_id: str | None = None):
    try:
        return APIResponse(data=service.snapshot(session_id), request_id=request_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error


@router.post("", response_model=APIResponse[PersistentTaskDTO])
async def create_task(payload: PersistentTaskCreateRequest, request_id: RequestId,
                      service: TaskService):
    return APIResponse(data=await _apply(lambda: service.create(payload)), request_id=request_id)


@router.get("/events", response_class=StreamingResponse,
            responses=sse_responses("持久任务初始快照与精确更新", {
                "snapshot": PersistentTaskListDTO, "changed": PersistentTaskChangedDTO, "error": SseErrorDTO,
            }))
async def task_events(service: TaskService, session_id: str | None = None):
    return StreamingResponse(stream_sse_with_heartbeat(
        open_persistent_task_sse(service, session_id), heartbeat_interval_seconds=SSE_HEARTBEAT_INTERVAL_SECONDS,
        close_source_on_exit=True), media_type="text/event-stream", headers=SSE_NO_CACHE_HEADERS)


async def open_persistent_task_sse(service: PersistentTaskService, session_id: str | None = None):
    """独立接口与工作区聚合流共享同一任务订阅及错误语义。"""
    channel = service.runtime.events

    subscription = channel.subscribe(label="persistent-task-management")
    try:
        snapshot = await run_sync_shielded(service.snapshot, session_id)
        yield "event: snapshot\ndata: " + snapshot.model_dump_json() + "\n\n"
        while True:
            delivery = await subscription.next()
            event = delivery.event
            if delivery.gap or event.task_id is None:
                snapshot = await run_sync_shielded(service.snapshot, session_id)
                snapshot = snapshot.model_copy(update={"history_invalidated": delivery.gap or event.history_invalidated})
                yield "event: snapshot\ndata: " + snapshot.model_dump_json() + "\n\n"
            else:
                changed = await run_sync_shielded(service.changed, event.task_id, event.run_id, session_id)
                yield "event: changed\ndata: " + changed.model_dump_json() + "\n\n"
    except (KeyError, NotFoundError) as error:
        # 会话已删除时显式结束订阅，不把业务缺失伪装成可重连的网络断线。
        failure = SseErrorDTO(message=client_error_message(error))
        yield "event: error\ndata: " + failure.model_dump_json() + "\n\n"
    finally:
        channel.unsubscribe(subscription)


@router.post("/imports/session-generators", response_model=APIResponse[dict[str, str]])
async def import_session_generator(payload: SessionGeneratorImportRequest, request_id: RequestId, service: TaskService):
    task_id = await _apply(lambda: import_generator(service.runtime, payload))
    return APIResponse(data={"task_id": task_id}, request_id=request_id)


@router.get("/{task_id}", response_model=APIResponse[PersistentTaskDTO])
def get_task(task_id: str, request_id: RequestId,
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
def task_runs(task_id: str, request_id: RequestId,
              service: TaskService, before: str | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 50):
    try:
        return APIResponse(data=service.runs(task_id, before=before, limit=limit), request_id=request_id)
    except KeyError as error:
        raise not_found_http_error(error) from error
    except (ValidationError, PydanticSerializationError):
        raise
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/{task_id}/notifications", response_model=APIResponse[PersistentTaskRunDTO])
async def notify_task(task_id: str, payload: PersistentTaskNotificationRequest, request_id: RequestId,
                      service: TaskService):
    return APIResponse(data=await _apply(lambda: service.check(task_id, payload)), request_id=request_id)


@router.get("/{task_id}/runs/{run_id}", response_model=APIResponse[PersistentTaskRunDetailDTO])
async def inspect_task_run(task_id: str, run_id: str, request_id: RequestId, service: TaskService):
    return APIResponse(data=await _apply(lambda: service.inspect_run(task_id, run_id)), request_id=request_id)
