from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import (
    get_message_stream_store,
    get_request_id,
    verify_local_token,
)
from app.protocol.codecs.message_stream import (
    message_stream_to_json,
    message_stream_to_proto,
)
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.message_stream_store import (
    MessageStreamCursorGoneError,
    MessageStreamError,
    MessageStreamNotFoundError,
    MessageStreamStore,
)

router = APIRouter(prefix="/sessions", tags=["message-stream"])


def _parse_after_seq(after_seq: int | None, last_event_id: str | None) -> int:
    if last_event_id is None:
        return max(after_seq or 0, 0)
    try:
        parsed = int(last_event_id)
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail="Last-Event-ID 必须是整数 event_seq",
        ) from error
    if after_seq is not None and after_seq != parsed:
        raise HTTPException(status_code=409, detail="after_seq 与 Last-Event-ID 不一致")
    return max(parsed, 0)


def _sse_frame(event: dict[str, object]) -> str:
    proto_event = message_stream_to_proto(event)
    value = message_stream_to_json(proto_event)
    return (
        f"id: {value['event_seq']}\n"
        f"event: {value['type']}\n"
        f"data: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _public_snapshot(
    *,
    session_id: str,
    turn_id: str,
    turn_stream_id: str,
    snapshot: dict[str, object],
) -> dict[str, object]:
    """通过 v1 编解码边界返回快照，避免内部 checkpoint 字段泄漏。"""
    value = message_stream_to_json(
        message_stream_to_proto(
            {
                "event_id": f"snapshot_{turn_stream_id}_{snapshot['snapshot_seq']}",
                "session_id": session_id,
                "turn_id": turn_id,
                "turn_stream_id": turn_stream_id,
                "event_seq": int(snapshot["snapshot_seq"]),
                "type": "stream.snapshot",
                "payload": snapshot,
            }
        )
    )
    payload = value["payload"]
    if not isinstance(payload, dict):
        raise MessageStreamError("消息流快照编解码结果不是对象")
    # HTTP 快照不是 SSE payload，仍需把资源定位键放回响应数据，供前端
    # 在首次连接或重连时确认它拿到的是目标 Turn 的快照。
    payload.update(
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "turn_stream_id": turn_stream_id,
        }
    )
    return payload


@router.get(
    "/{session_id}/turns/{turn_id}/message-stream",
    response_class=StreamingResponse,
    summary="订阅 Turn 消息流",
)
async def stream_message_events(
    session_id: str,
    turn_id: str,
    request: Request,
    turn_stream_id: str | None = Query(default=None),
    after_seq: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    store: MessageStreamStore = Depends(get_message_stream_store),  # noqa: B008
):
    cursor = _parse_after_seq(after_seq, last_event_id)
    try:
        writer = await store.open_existing(
            session_id=session_id,
            turn_id=turn_id,
            turn_stream_id=turn_stream_id,
        )
    except (MessageStreamError, FileNotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    async def event_generator():
        async for event in store.stream_records(
            session_id=session_id,
            turn_stream_id=writer.turn_stream_id,
            after_seq=cursor,
        ):
            if await request.is_disconnected():
                break
            yield _sse_frame(event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Message-Stream-ID": writer.turn_stream_id,
            "X-Request-ID": request_id,
        },
    )


@router.get(
    "/{session_id}/turns/{turn_id}/message-stream/snapshot",
    response_model=APIResponse[dict[str, object]],
    summary="获取 Turn 消息流快照",
)
async def get_message_stream_snapshot(
    session_id: str,
    turn_id: str,
    turn_stream_id: str | None = Query(default=None),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    store: MessageStreamStore = Depends(get_message_stream_store),  # noqa: B008
):
    try:
        writer = await store.open_existing(
            session_id=session_id,
            turn_id=turn_id,
            turn_stream_id=turn_stream_id,
        )
        snapshot = await store.get_state(writer.turn_stream_id)
    except (MessageStreamNotFoundError, MessageStreamError, FileNotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=_public_snapshot(
            session_id=session_id,
            turn_id=turn_id,
            turn_stream_id=writer.turn_stream_id,
            snapshot=snapshot,
        ),
        request_id=request_id,
    )


@router.get(
    "/{session_id}/turns/{turn_id}/message-stream/events",
    response_model=APIResponse[list[dict[str, object]]],
    summary="获取 Turn 消息流事件",
)
async def list_message_stream_events(
    session_id: str,
    turn_id: str,
    turn_stream_id: str | None = Query(default=None),
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    store: MessageStreamStore = Depends(get_message_stream_store),  # noqa: B008
):
    try:
        writer = await store.open_existing(
            session_id=session_id,
            turn_id=turn_id,
            turn_stream_id=turn_stream_id,
        )
        events = await store.list_events(
            session_id=session_id,
            turn_stream_id=writer.turn_stream_id,
            after_seq=after_seq,
            limit=limit,
        )
    except MessageStreamCursorGoneError as error:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "message_stream_cursor_gone",
                "message": str(error),
                "turn_stream_id": error.turn_stream_id,
                "first_seq": error.first_seq,
            },
        ) from error
    except (MessageStreamNotFoundError, MessageStreamError, FileNotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=[
            message_stream_to_json(message_stream_to_proto(event))
            for event in events
        ],
        request_id=request_id,
    )
