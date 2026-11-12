from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import (
    get_config_service,
    get_session_catalog_service,
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
from app.services.business.session_navigation.service import SessionCatalogService
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
    _: Annotated[str, Depends(verify_local_token)],
    activity_service: Annotated[WorkspaceActivityService, Depends(get_workspace_activity_service)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
    catalog_service: Annotated[SessionCatalogService, Depends(get_session_catalog_service)],
    config_service: Annotated[ConfigService, Depends(get_config_service)],
    watch_service: Annotated[WorkspaceFileWatchService, Depends(get_workspace_file_watch_service)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
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
            if subscription.kind == "session_navigation":
                return OpenedWorkspaceObservation(_stream_navigation_changes(catalog_service))
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


async def _stream_navigation_changes(service: SessionCatalogService):
    async with aclosing(stream_sse_with_heartbeat(
        service.operations.changes(), heartbeat_interval_seconds=SSE_HEARTBEAT_INTERVAL_SECONDS,
        close_source_on_exit=True,
    )) as changes:
        async for snapshot in changes:
            if isinstance(snapshot, str):
                yield snapshot
            else:
                yield f"event: session_navigation\ndata: {snapshot.model_dump_json()}\n\n"
