from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.deps import (
    get_request_id,
    get_workspace_activity_service,
    verify_local_token,
)
from app.schemas.public_v2.common import APIResponse, CursorPage
from app.services.infrastructure.workspace_state_store import (
    WorkspaceActivityCursorGoneError,
    WorkspaceActivityRecord,
    WorkspaceActivityService,
)
from pydantic import BaseModel


class SessionActivityDTO(BaseModel):
    event_seq: int
    event_id: str
    session_id: str
    status: str
    summary: str
    occurred_at: str


router = APIRouter(prefix="/session-catalog/events", tags=["session-catalog-events"])


def _dto(record: WorkspaceActivityRecord) -> SessionActivityDTO:
    return SessionActivityDTO(
        event_seq=record.event_seq,
        event_id=record.event_id,
        session_id=record.session_id,
        status=record.status,
        summary=record.summary,
        occurred_at=record.occurred_at,
    )


@router.get("", response_model=APIResponse[CursorPage[SessionActivityDTO]])
async def list_session_activity(
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    service: WorkspaceActivityService = Depends(get_workspace_activity_service),
):
    try:
        records = service.list(after=after, limit=limit + 1)
    except WorkspaceActivityCursorGoneError as error:
        raise HTTPException(status_code=410, detail=str(error)) from error
    has_more = len(records) > limit
    page = records[:limit]
    return APIResponse(
        data=CursorPage(
            items=[_dto(record) for record in page],
            next_cursor=str(page[-1].event_seq) if has_more and page else None,
            has_more=has_more,
        ),
        request_id=request_id,
    )


@router.get("/stream")
async def stream_session_activity(
    after: int = Query(default=0, ge=0),
    _: str = Depends(verify_local_token),
    service: WorkspaceActivityService = Depends(get_workspace_activity_service),
):
    async def generate():
        try:
            async for record in service.stream(after=after):
                if record is None:
                    yield ": ping\n\n"
                    continue
                payload = json.dumps(_dto(record).model_dump(mode="json"), ensure_ascii=False)
                yield f"id: {record.event_seq}\nevent: session_activity\ndata: {payload}\n\n"
        except WorkspaceActivityCursorGoneError as error:
            yield f"event: cursor_gone\ndata: {json.dumps({'message': str(error)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
