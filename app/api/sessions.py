from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.abstractions.job_service import JobServiceProtocol
from app.api.deps import (
    get_config_service,
    get_context_compaction_service,
    get_file_tree_settings_service,
    get_goal_runtime_service,
    get_goal_service,
    get_job_service,
    get_llm_request_log_service,
    get_request_id,
    get_session_changes_service,
    get_session_context_fork_service,
    get_session_information_service,
    get_session_interrupt_service,
    get_session_resource_service,
    get_session_service,
    verify_local_token,
)
from app.core.exceptions import NotFoundError
from app.protocol.codecs.workspace_events import trace_to_json, trace_to_proto
from app.schemas.internal_v2.common import APIResponse, CursorPage
from app.schemas.internal_v2.goal import (
    SessionGoalClearResultDTO,
    SessionGoalDTO,
    SessionGoalSetRequest,
)
from app.schemas.internal_v2.llm_request_log import LLMRequestLogRecordDTO
from app.schemas.internal_v2.session import (
    DeleteSessionResultDTO,
    SessionCompactResultDTO,
    SessionCreateRequest,
    SessionDTO,
    SessionInformationSnapshotDTO,
    SessionInterruptResultDTO,
    SessionForkRequest,
    SessionUpdateRequest,
)
from app.schemas.internal_v2.session_changes import (
    SessionChangesetDTO,
    SessionChangesetListDTO,
    SessionFileReviewRequest,
    SessionFileReviewResultDTO,
)
from app.schemas.internal_v2.session_resource import (
    SessionResourceControlRequest,
    SessionResourceControlResultDTO,
    SessionResourceKind,
    SessionResourceListDTO,
)
from app.schemas.internal_v2.sse import sse_responses
from app.schemas.internal_v2.trace import TraceEventDTO
from app.schemas.internal_v2.workspace import (
    FileTreeShortcutRequest,
    SessionFileTreeSettingsDTO,
)
from app.services.business.context_compaction_service import ContextCompactionService
from app.services.business.session_changes_service import SessionChangesService
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_goal_service import (
    TOKEN_BUDGET_UNSET,
    SessionGoalService,
)
from app.services.business.session_information_service import SessionInformationService
from app.services.business.session_interrupt_service import SessionInterruptService
from app.services.business.session_resource_service import SessionResourceService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.file_tree_settings_service import (
    FileTreeSettingsService,
)
from app.services.infrastructure.llm_request_log_service import LLMRequestLogService
from app.services.infrastructure.trace_event_store import TraceCursorGoneError
from app.services.infrastructure.turn_history.trace_page import (
    TracePageBudgetExceededError,
)
from app.services.orchestration.goal_runtime_service import GoalRuntimeService

router = APIRouter(prefix="/sessions", tags=["sessions"])
TRACE_STREAM_HEARTBEAT_INTERVAL_SECONDS = 15.0


@router.get(
    "/{session_id}/goal",
    response_model=APIResponse[SessionGoalDTO | None],
    summary="获取会话 Goal",
)
async def get_session_goal(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    goal_service: SessionGoalService = Depends(get_goal_service),
):
    return APIResponse(data=await goal_service.get(session_id), request_id=request_id)


@router.put(
    "/{session_id}/goal",
    response_model=APIResponse[SessionGoalDTO],
    summary="设置会话 Goal",
)
async def set_session_goal(
    session_id: str,
    payload: SessionGoalSetRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    goal_service: SessionGoalService = Depends(get_goal_service),
    runtime: GoalRuntimeService = Depends(get_goal_runtime_service),
):
    token_budget = (
        payload.token_budget
        if "token_budget" in payload.model_fields_set
        else TOKEN_BUDGET_UNSET
    )
    try:
        previous_goal = await goal_service.get(session_id)
        if payload.replace or (
            payload.status is not None and payload.status.value != "active"
        ):
            await runtime.settle_active_progress(session_id)
        goal = await goal_service.set(
            session_id,
            objective=payload.objective,
            status=payload.status,
            token_budget=token_budget,
            replace=payload.replace,
        )
        if goal.status.value == "active":
            if (
                not payload.replace
                and payload.objective is not None
                and previous_goal is not None
                and previous_goal.objective != goal.objective
            ):
                await runtime.apply_objective_update(goal)
            await runtime.ensure_active_goal_running(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return APIResponse(data=goal, request_id=request_id)


@router.delete(
    "/{session_id}/goal",
    response_model=APIResponse[SessionGoalClearResultDTO],
    summary="清除会话 Goal",
)
async def clear_session_goal(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    goal_service: SessionGoalService = Depends(get_goal_service),
    runtime: GoalRuntimeService = Depends(get_goal_runtime_service),
):
    await runtime.settle_active_progress(session_id)
    cleared = await goal_service.clear(session_id)
    return APIResponse(
        data=SessionGoalClearResultDTO(session_id=session_id, cleared=cleared),
        request_id=request_id,
    )


@router.post("", response_model=APIResponse[SessionDTO], summary="创建会话")
async def create_session(
    payload: SessionCreateRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
):
    try:
        result = await session_service.create(payload)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "", response_model=APIResponse[CursorPage[SessionDTO]], summary="获取会话列表"
)
async def list_sessions(
    limit: int = 20,
    cursor: str | None = None,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
):
    result = await session_service.list(limit=limit, cursor=cursor)
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}", response_model=APIResponse[SessionDTO], summary="获取会话详情"
)
async def get_session(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
):
    try:
        result = await session_service.get(session_id)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error.detail)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/information",
    response_model=APIResponse[SessionInformationSnapshotDTO],
    summary="获取通用会话信息",
)
async def get_session_information(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    information_service: SessionInformationService = Depends(
        get_session_information_service
    ),
):
    result = await information_service.get_information(session_id)
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/fork-context",
    response_model=APIResponse[SessionDTO],
    summary="复制 Agent 上下文状态并创建子会话",
)
async def fork_session_context(
    session_id: str,
    payload: SessionForkRequest | None = None,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    fork_service: SessionContextForkService = Depends(get_session_context_fork_service),
):
    request = payload or SessionForkRequest()
    result = await fork_service.fork(
        session_id,
        mode=request.mode,
        turn_id=request.turn_id,
        anchor_mode=request.anchor_mode,
        pinned=request.pinned,
        place_under_source=request.pinned,
    )
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/traces",
    response_model=APIResponse[CursorPage[TraceEventDTO]],
    summary="分页获取会话执行轨迹",
)
async def list_session_traces(
    session_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
):
    try:
        result = await session_service.list_trace_events(
            session_id,
            cursor=cursor,
            limit=limit,
        )
    except TraceCursorGoneError as exc:
        raise _trace_cursor_gone_http_error(exc) from exc
    except TracePageBudgetExceededError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/llm-request-logs",
    response_model=APIResponse[list[LLMRequestLogRecordDTO]],
    summary="获取会话完整 LLM 请求响应日志",
)
async def list_session_llm_request_logs(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    llm_request_log_service: LLMRequestLogService = Depends(
        get_llm_request_log_service
    ),
):
    result = llm_request_log_service.list_session_logs(session_id)
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/resources",
    response_model=APIResponse[SessionResourceListDTO],
    summary="获取会话后台连接列表",
)
async def list_session_resources(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_resource_service: SessionResourceService = Depends(
        get_session_resource_service
    ),
):
    try:
        result = await session_resource_service.list(session_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/changesets",
    response_model=APIResponse[SessionChangesetListDTO],
    summary="获取会话文件变更视图列表",
)
async def list_session_changesets(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_changes_service: SessionChangesService = Depends(
        get_session_changes_service
    ),
):
    result = await session_changes_service.list_changesets(session_id)
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/changesets/{changeset_id}",
    response_model=APIResponse[SessionChangesetDTO],
    summary="获取会话文件变更详情",
)
async def get_session_changeset(
    session_id: str,
    changeset_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_changes_service: SessionChangesService = Depends(
        get_session_changes_service
    ),
):
    try:
        result = await session_changes_service.get_changeset(
            session_id=session_id,
            changeset_id=changeset_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/changesets/{changeset_id}/review",
    response_model=APIResponse[SessionFileReviewResultDTO],
    summary="标记或取消标记会话文件变更已审查",
)
async def review_session_changeset_file(
    session_id: str,
    changeset_id: str,
    payload: SessionFileReviewRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_changes_service: SessionChangesService = Depends(
        get_session_changes_service
    ),
):
    del changeset_id
    try:
        result = await session_changes_service.set_file_reviewed(
            session_id=session_id,
            file_path=payload.file_path,
            reviewed=payload.reviewed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/resources/{kind}/{resource_id}/control",
    response_model=APIResponse[SessionResourceControlResultDTO],
    summary="控制会话后台连接",
)
async def control_session_resource(
    session_id: str,
    kind: SessionResourceKind,
    resource_id: str,
    payload: SessionResourceControlRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_resource_service: SessionResourceService = Depends(
        get_session_resource_service
    ),
):
    try:
        result = await session_resource_service.control(
            session_id=session_id,
            kind=kind,
            resource_id=resource_id,
            action=payload.action,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/traces/stream",
    response_class=StreamingResponse,
    summary="订阅会话执行轨迹流",
    responses=sse_responses("SSE Trace 事件流", {"trace": TraceEventDTO}),
)
async def stream_session_traces(
    session_id: str,
    after_event_id: str | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _: str = Depends(verify_local_token),
    session_service: SessionService = Depends(get_session_service),
    config_service: ConfigService = Depends(get_config_service),
):
    if after_event_id and last_event_id and after_event_id != last_event_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "trace_cursor_conflict",
                "message": "after_event_id 与 Last-Event-ID 不一致",
                "session_id": session_id,
            },
        )
    cursor = last_event_id or after_event_id
    try:
        await session_service.ensure_trace_cursor(session_id, cursor)
    except TraceCursorGoneError as exc:
        raise _trace_cursor_gone_http_error(exc) from exc

    events = session_service.stream_trace_events(session_id, cursor)
    return StreamingResponse(
        _stream_trace_sse(
            events,
            heartbeat_interval_seconds=(
                config_service.get_trace_stream_heartbeat_interval_seconds()
            ),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_trace_sse(
    events: AsyncIterator[tuple[TraceEventDTO, str]],
    *,
    heartbeat_interval_seconds: float = TRACE_STREAM_HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[str]:
    """在真实 Trace 事件之间发送 SSE 注释心跳。"""
    if heartbeat_interval_seconds <= 0:
        raise ValueError("SSE 心跳间隔必须大于 0")

    iterator = aiter(events)
    next_event = asyncio.create_task(anext(iterator))
    try:
        while True:
            completed, _ = await asyncio.wait(
                {next_event},
                timeout=heartbeat_interval_seconds,
            )
            if not completed:
                yield ": heartbeat\n\n"
                continue

            try:
                event, cursor = next_event.result()
            except StopAsyncIteration:
                return

            event_payload = event.model_dump(mode="json")
            data = json.dumps(
                trace_to_json(trace_to_proto(event_payload)),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"id: {cursor}\nevent: trace\ndata: {data}\n\n"
            next_event = asyncio.create_task(anext(iterator))
    finally:
        if not next_event.done():
            next_event.cancel()
            with suppress(asyncio.CancelledError):
                await next_event


def _trace_cursor_gone_http_error(exc: TraceCursorGoneError) -> HTTPException:
    return HTTPException(
        status_code=410,
        detail={
            "code": "trace_cursor_gone",
            "message": "事件游标已不在当前会话历史中",
            "session_id": exc.session_id,
            "requested_cursor": exc.cursor,
            "recovery": "reload_snapshot",
        },
    )


@router.get(
    "/{session_id}/file-tree-settings",
    response_model=APIResponse[SessionFileTreeSettingsDTO],
    summary="获取会话文件树快捷路径配置",
)
async def get_session_file_tree_settings(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: FileTreeSettingsService = Depends(get_file_tree_settings_service),
):
    try:
        result = service.get(session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/file-tree-shortcuts",
    response_model=APIResponse[SessionFileTreeSettingsDTO],
    summary="添加会话级文件树快捷路径",
)
async def add_session_file_tree_shortcut(
    session_id: str,
    payload: FileTreeShortcutRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: FileTreeSettingsService = Depends(get_file_tree_settings_service),
):
    try:
        result = service.add_session_shortcut(
            session_id,
            path=payload.path,
            label=payload.label,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.delete(
    "/{session_id}/file-tree-shortcuts",
    response_model=APIResponse[SessionFileTreeSettingsDTO],
    summary="删除会话级文件树快捷路径",
)
async def remove_session_file_tree_shortcut(
    session_id: str,
    path: str = Query(min_length=1, max_length=4096),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: FileTreeSettingsService = Depends(get_file_tree_settings_service),
):
    try:
        result = service.remove_session_shortcut(session_id, path=path)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/file-tree-shortcuts/apply-to-workspace",
    response_model=APIResponse[SessionFileTreeSettingsDTO],
    summary="将会话快捷路径设为新会话默认值",
)
async def apply_file_tree_shortcut_to_workspace(
    session_id: str,
    payload: FileTreeShortcutRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: FileTreeSettingsService = Depends(get_file_tree_settings_service),
):
    try:
        result = service.apply_to_workspace(
            session_id,
            path=payload.path,
            label=payload.label,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.delete(
    "/{session_id}/workspace-file-tree-shortcuts",
    response_model=APIResponse[SessionFileTreeSettingsDTO],
    summary="删除新会话默认文件树快捷路径",
)
async def remove_workspace_file_tree_shortcut(
    session_id: str,
    path: str = Query(min_length=1, max_length=4096),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: FileTreeSettingsService = Depends(get_file_tree_settings_service),
):
    try:
        result = service.remove_workspace_shortcut(session_id, path=path)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.patch(
    "/{session_id}", response_model=APIResponse[SessionDTO], summary="更新会话"
)
async def update_session(
    session_id: str,
    payload: SessionUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
):
    try:
        result = await session_service.update(session_id, payload)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc.detail)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return APIResponse(data=result, request_id=request_id)


@router.delete(
    "/{session_id}",
    response_model=APIResponse[DeleteSessionResultDTO],
    summary="删除会话",
)
async def delete_session(
    session_id: str,
    cascade: bool = Query(default=False),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
    session_resource_service: SessionResourceService = Depends(
        get_session_resource_service
    ),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    async def cleanup_and_delete() -> DeleteSessionResultDTO:
        cleaned_execution_runs = 0
        cleaned_background_tasks = 0
        cleaned_terminals = 0
        for target_session_id in session_ids:
            cleanup_result = await session_resource_service.cleanup_session(
                target_session_id
            )
            cleaned_execution_runs += cleanup_result.cleaned_execution_runs
            cleaned_background_tasks += cleanup_result.cleaned_background_tasks
            cleaned_terminals += cleanup_result.cleaned_terminals
        return (await session_service.delete(session_id, cascade=cascade)).model_copy(
            update={
                "cleaned_execution_runs": cleaned_execution_runs,
                "cleaned_background_tasks": cleaned_background_tasks,
                "cleaned_terminals": cleaned_terminals,
            }
        )

    try:
        session_ids = session_service.path_resolver.descendant_session_ids(
            session_id,
            include_self=True,
        )
        result = await job_service.run_sessions_delete_operation(
            session_ids,
            cleanup_and_delete,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/compact",
    response_model=APIResponse[SessionCompactResultDTO],
    summary="压缩会话上下文",
)
async def compact_session_context(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    context_compaction_service: ContextCompactionService = Depends(
        get_context_compaction_service
    ),
):
    result = await context_compaction_service.compact(session_id=session_id)
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/interrupt",
    response_model=APIResponse[SessionInterruptResultDTO],
    summary="打断会话正在执行的任务",
)
async def interrupt_session(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    session_interrupt_service: SessionInterruptService = Depends(
        get_session_interrupt_service
    ),
):
    try:
        result = await session_interrupt_service.interrupt(session_id=session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return APIResponse(data=result, request_id=request_id)
