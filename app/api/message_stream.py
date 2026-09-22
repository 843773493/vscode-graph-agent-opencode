from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from google.protobuf import json_format
from pydantic import ValidationError

from app.api.canonical_params import CanonicalSessionId
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
from app.schemas.internal_v2.message_stream import MessageStreamSnapshotDTO
from app.services.infrastructure.message_stream_store import (
    MessageStreamCursorGoneError,
    MessageStreamError,
    MessageStreamNotFoundError,
    MessageStreamStore,
)

router = APIRouter(prefix="/sessions", tags=["message-stream"])
MessageStreamEventRecord = Mapping[str, object]
# 空闲心跳：与 trace 流、workspace 文件流保持同一口径（15s 间隔 + `: heartbeat` 注释）。
# 该流在长工具运行期间会长时间没有新事件，若无心跳前端无法设置空闲阈值。
MESSAGE_STREAM_HEARTBEAT_INTERVAL_SECONDS = 15.0
# stream.snapshot 的定位字段只保留在事件信封中，payload 不重复承载。
_SNAPSHOT_ENVELOPE_FIELDS = ("session_id", "turn_id", "turn_stream_id")


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


def _sse_frame(event: MessageStreamEventRecord) -> str:
    value = _public_event_json(event)
    return (
        f"id: {value['event_seq']}\n"
        f"event: {value['type']}\n"
        f"data: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _public_event_json(event: MessageStreamEventRecord) -> dict[str, object]:
    """SSE 与 HTTP 共用的公共线格式投影。"""
    value = message_stream_to_json(message_stream_to_proto(event))
    if value["type"] != "stream.snapshot":
        return value
    snapshot = _snapshot_projection(event).model_dump(mode="json", exclude_none=True)
    for field_name in _SNAPSHOT_ENVELOPE_FIELDS:
        snapshot.pop(field_name, None)
    return {**value, "payload": snapshot}


def _snapshot_projection(event: MessageStreamEventRecord) -> MessageStreamSnapshotDTO:
    """stream.snapshot 的唯一公共投影：SSE 控制帧与 HTTP 快照共用同一份实现。"""
    try:
        value = message_stream_to_json(message_stream_to_proto(event))
    except (TypeError, ValueError, json_format.ParseError) as error:
        raise MessageStreamError("消息流快照编解码失败") from error
    payload = value["payload"]
    if not isinstance(payload, Mapping):
        raise MessageStreamError("消息流快照编解码结果不是对象")
    source = event.get("payload")
    if not isinstance(source, Mapping):
        raise MessageStreamError("消息流快照源数据不是对象")
    projected = dict(payload)
    # 快照序号、尝试次数和可恢复性是存储状态的标量来源；protobuf JSON 为零值
    # 省略它们，公共 DTO 仍需把同一真实值明确返回。
    projected.update(
        {
            "session_id": event["session_id"],
            "turn_id": event["turn_id"],
            "turn_stream_id": event["turn_stream_id"],
            "snapshot_seq": source["snapshot_seq"],
            "current_attempt": source["current_attempt"],
            "resumable": source["resumable"],
            "stream_status": source["stream_status"],
            "agent_loop_status": source["agent_loop_status"],
        }
    )
    try:
        return MessageStreamSnapshotDTO.model_validate(projected)
    except ValidationError as error:
        raise MessageStreamError("消息流快照不符合公共 DTO") from error


def _public_snapshot(
    *,
    session_id: CanonicalSessionId,
    turn_id: str,
    turn_stream_id: str,
    snapshot: Mapping[str, object],
) -> MessageStreamSnapshotDTO:
    """通过 v1 编解码边界返回快照，避免内部 checkpoint 字段泄漏。"""
    event = {
        "event_id": f"snapshot_{turn_stream_id}_{snapshot['snapshot_seq']}",
        "session_id": session_id,
        "turn_id": turn_id,
        "turn_stream_id": turn_stream_id,
        "event_seq": int(snapshot["snapshot_seq"]),
        "type": "stream.snapshot",
        "payload": snapshot,
    }
    return _snapshot_projection(event)


async def _stream_message_sse(
    records: AsyncIterator[MessageStreamEventRecord],
    *,
    request: Request,
    heartbeat_interval_seconds: float = MESSAGE_STREAM_HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[str]:
    """在真实消息流事件之间发送 SSE 注释心跳。

    与 ``_stream_trace_sse``、workspace 文件流同形：事件任务与心跳超时竞争，
    超时即发 ``: heartbeat`` 注释帧（不是业务事件），客户端断开时立即停止。
    """
    iterator = aiter(records)
    next_record = asyncio.create_task(anext(iterator))
    try:
        while True:
            completed, _ = await asyncio.wait(
                {next_record},
                timeout=heartbeat_interval_seconds,
            )
            if not completed:
                if await request.is_disconnected():
                    return
                yield ": heartbeat\n\n"
                continue
            try:
                event = next_record.result()
            except StopAsyncIteration:
                return
            if await request.is_disconnected():
                return
            yield _sse_frame(event)
            next_record = asyncio.create_task(anext(iterator))
    finally:
        if not next_record.done():
            next_record.cancel()
            with suppress(asyncio.CancelledError):
                await next_record
        await iterator.aclose()


@router.get(
    "/{session_id}/message-streams/availability",
    response_model=APIResponse[dict[str, str]],
    summary="查询 Turn 已持久化的消息流",
)
async def get_message_stream_availability(
    session_id: CanonicalSessionId,
    turn_ids: list[str] = Query(min_length=1, max_length=4),  # noqa: B008
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    store: MessageStreamStore = Depends(get_message_stream_store),  # noqa: B008
):
    streams = await store.existing_stream_ids(
        session_id=session_id,
        turn_ids=list(dict.fromkeys(turn_ids)),
    )
    return APIResponse(data=streams, request_id=request_id)


@router.get(
    "/{session_id}/turns/{turn_id}/message-stream",
    response_class=StreamingResponse,
    summary="订阅 Turn 消息流",
)
async def stream_message_events(
    session_id: CanonicalSessionId,
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

    return StreamingResponse(
        _stream_message_sse(
            store.stream_records(
                session_id=session_id,
                turn_stream_id=writer.turn_stream_id,
                after_seq=cursor,
            ),
            request=request,
            heartbeat_interval_seconds=MESSAGE_STREAM_HEARTBEAT_INTERVAL_SECONDS,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Message-Stream-ID": writer.turn_stream_id,
            "X-Request-ID": request_id,
        },
    )


@router.get(
    "/{session_id}/turns/{turn_id}/message-stream/snapshot",
    response_model=APIResponse[MessageStreamSnapshotDTO],
    response_model_exclude_none=True,
    summary="获取 Turn 消息流快照",
)
async def get_message_stream_snapshot(
    session_id: CanonicalSessionId,
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
    response_model=APIResponse[list[MessageStreamEventRecord]],
    summary="获取 Turn 消息流事件",
)
async def list_message_stream_events(
    session_id: CanonicalSessionId,
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
        data=[_public_event_json(event) for event in events],
        request_id=request_id,
    )
