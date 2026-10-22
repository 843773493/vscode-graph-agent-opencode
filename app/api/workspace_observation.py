from __future__ import annotations

import asyncio
import json
from contextlib import aclosing

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import (
    get_config_service,
    get_session_service,
    get_workspace_activity_service,
    get_workspace_file_watch_service,
    get_workspace_service,
    verify_local_token,
)
from app.api.session_activity import _stream_session_activity_sse
from app.api.sessions import open_session_trace_sse
from app.api.sse_heartbeat import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    SSE_NO_CACHE_HEADERS,
    stream_sse_with_heartbeat,
)
from app.api.workspace import open_workspace_file_sse
from app.schemas.internal_v2.sse import sse_responses
from app.schemas.internal_v2.workspace import WorkspaceFileWatchRequest
from app.schemas.internal_v2.workspace_observation import (
    WorkspaceObservationFrameDTO,
    WorkspaceObservationRequest,
    WorkspaceObservationSubscription,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)
from app.services.infrastructure.workspace_service import WorkspaceService
from app.services.infrastructure.workspace_state_store import WorkspaceActivityService
from app.services.orchestration.workspace_observation import (
    OpenedWorkspaceObservation,
    WorkspaceObservationError,
    WorkspaceObservationSource,
    stream_workspace_observations,
)

router = APIRouter(prefix="/workspace/observations", tags=["workspace-observations"])


@router.post(
    "/stream",
    response_class=StreamingResponse,
    summary="通过单一物理连接订阅独立工作区观察者",
    responses=sse_responses(
        "SSE 工作区观察者传输流", {"observation": WorkspaceObservationFrameDTO},
    ),
)
async def stream_workspace_observation(
    payload: WorkspaceObservationRequest,
    _: str = Depends(verify_local_token),
    activity_service: WorkspaceActivityService = Depends(get_workspace_activity_service),
    session_service: SessionService = Depends(get_session_service),
    config_service: ConfigService = Depends(get_config_service),
    watch_service: WorkspaceFileWatchService = Depends(get_workspace_file_watch_service),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    async def open_source(subscription: WorkspaceObservationSubscription) -> OpenedWorkspaceObservation:
        try:
            if subscription.kind == "session_activity":
                return OpenedWorkspaceObservation(_stream_session_activity_sse(
                    activity_service, after=subscription.activity_after or 0,
                ))
            if subscription.kind == "session_trace":
                assert subscription.session_id is not None
                return OpenedWorkspaceObservation(await open_session_trace_sse(
                    subscription.session_id, subscription.trace_after, session_service, config_service,
                    thread_id=subscription.thread_id,
                ))
            cursor = subscription.files_after
            if cursor is None:
                cursor = workspace_service.file_operations.store.latest_event_id()
            return OpenedWorkspaceObservation(
                await asyncio.to_thread(open_workspace_file_sse,
                    WorkspaceFileWatchRequest(paths=subscription.paths, after_event_id=cursor),
                    watch_service, workspace_service,
                ),
                cursor=str(cursor),
            )
        except HTTPException as error:
            raise WorkspaceObservationError(error.status_code, error.detail) from error

    sources = []
    for subscription in payload.subscriptions:
        async def open_selected(selected=subscription):
            return await open_source(selected)
        sources.append(WorkspaceObservationSource(subscription.subscription_id, open_selected))

    async def generate():
        async with aclosing(stream_sse_with_heartbeat(
            stream_workspace_observations(sources),
            heartbeat_interval_seconds=SSE_HEARTBEAT_INTERVAL_SECONDS,
            close_source_on_exit=True,
        )) as stream:
            async for item in stream:
                if isinstance(item, str):
                    yield item
                    continue
                data = json.dumps(item.model_dump(mode="json", exclude_none=True),
                                  ensure_ascii=False, separators=(",", ":"))
                yield f"event: observation\ndata: {data}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream", headers=SSE_NO_CACHE_HEADERS)
