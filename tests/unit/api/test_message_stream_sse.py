from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy

import pytest

from app.api import message_stream as message_stream_api
from app.api.message_stream import _stream_message_sse

SESSION_ID = "ses_019bef80066f7038ae8e88ccb2e5e2ea"
THREAD_ID = "thr_019bef80066f7038ae8e88ccb2e5e2ea"


class _Request:
    """只实现 ``_stream_message_sse`` 需要的断开探测。"""

    def __init__(self, disconnected: bool = False) -> None:
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


class _IdleRecords:
    """永不产出事件的记录流，用于观测空闲心跳。"""

    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> _IdleRecords:
        return self

    async def __anext__(self) -> Mapping[str, object]:
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


def test_public_snapshot_event_reuses_protobuf_json_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = {
        "event_id": "evt_snapshot",
        "session_id": SESSION_ID,
        "thread_id": "thr_019bef80066f7038ae8e88ccb2e5e2ea",
        "turn_id": "job_snapshot",
        "turn_stream_id": "str_snapshot",
        "event_seq": 7,
        "type": "stream.snapshot",
        "payload": {
            "snapshot_seq": 7,
            "current_attempt": 0,
            "resumable": True,
            "stream_status": "open",
            "agent_loop_status": "running",
        },
    }
    serialized = {
        "type": "stream.snapshot",
        "payload": {"stream_status": "open", "agent_loop_status": "running"},
    }
    json_projection_calls = 0

    monkeypatch.setattr(
        message_stream_api,
        "message_stream_to_proto",
        lambda _event: object(),
    )

    def serialize_event(_proto: object) -> dict[str, object]:
        nonlocal json_projection_calls
        json_projection_calls += 1
        return serialized

    monkeypatch.setattr(message_stream_api, "message_stream_to_json", serialize_event)

    result = message_stream_api._public_event_json(event)

    assert json_projection_calls == 1
    assert result == {
        "type": "stream.snapshot",
        "payload": {
            "snapshot_seq": 7,
            "stream_status": "open",
            "agent_loop_status": "running",
            "current_attempt": 0,
            "resumable": True,
            "blocks": [],
            "tool_executions": [],
            "tool_calls": [],
            "model_calls": [],
            "activities": [],
            "resource_refs": [],
        },
    }


@pytest.mark.parametrize("sequence", [0, 2**53 + 1])
def test_http_and_sse_snapshot_projections_share_exact_integer_values(sequence: int) -> None:
    dynamic = {"started_seq": "9007199254740993", "snapshot_seq": "0"}
    event = {
        "event_id": "evt_snapshot", "session_id": SESSION_ID, "thread_id": THREAD_ID,
        "turn_id": "job_snapshot", "turn_stream_id": "str_snapshot", "event_seq": sequence,
        "type": "stream.snapshot", "payload": {
            "thread_id": THREAD_ID, "snapshot_seq": sequence, "current_attempt": 0,
            "resumable": False, "stream_status": "completed", "agent_loop_status": "idle",
            "blocks": [{"block_id": "block_1", "started_seq": sequence, "last_event_seq": sequence,
                        "completed_seq": sequence, "items": [dynamic]}],
            "interrupt_request_id": "interrupt_1", "interrupt_status": "confirmed",
        },
    }
    original = deepcopy(event)
    snapshot = message_stream_api._public_snapshot(
        session_id=SESSION_ID, turn_id="job_snapshot", turn_stream_id="str_snapshot", snapshot=event["payload"],
    ).model_dump(mode="json", exclude_none=True)
    frame = message_stream_api._sse_frame(event)
    assert frame.startswith(f"id: {sequence}\nevent: stream.snapshot\ndata: ")
    streamed = json.loads(frame.split("data: ", 1)[1])
    assert type(streamed["event_seq"]) is int and streamed["event_seq"] == sequence
    for field in ("session_id", "thread_id", "turn_id", "turn_stream_id"):
        assert streamed[field] == snapshot.pop(field)
    assert streamed["payload"] == snapshot
    assert snapshot["snapshot_seq"] == sequence and type(snapshot["snapshot_seq"]) is int
    assert snapshot["blocks"][0]["completed_seq"] == sequence
    assert type(snapshot["blocks"][0]["completed_seq"]) is int
    if sequence:
        assert snapshot["blocks"][0]["started_seq"] == sequence
        assert type(snapshot["blocks"][0]["started_seq"]) is int
    assert snapshot["blocks"][0]["items"] == [dynamic]
    assert snapshot["interrupt_state"] == {"request_id": "interrupt_1", "status": "confirmed"}
    assert event == original


@pytest.mark.asyncio
async def test_message_stream_sse_emits_heartbeat_while_idle() -> None:
    """无事件时在心跳间隔后收到 SSE 注释心跳。"""
    records = _IdleRecords()
    stream = _stream_message_sse(
        records,
        request=_Request(),
        heartbeat_interval_seconds=0.01,
    )

    frame = await asyncio.wait_for(anext(stream), timeout=0.5)

    assert frame == ": heartbeat\n\n"
    # 心跳必须是 SSE 注释，不得伪装成业务事件（否则前端会收到未知 type）。
    assert "event:" not in frame
    assert "data:" not in frame
    await stream.aclose()
    assert records.closed is True


@pytest.mark.asyncio
async def test_message_stream_sse_emits_event_block_without_heartbeat() -> None:
    """有事件时输出完整事件帧，且不掺入心跳。"""
    identity = {
        "session_id": SESSION_ID,
        "thread_id": THREAD_ID,
        "turn_id": "job_sse_event",
        "turn_stream_id": "str_sse_event",
    }
    events = [
        {
            **identity,
            "event_id": "evt_sse_opened",
            "event_seq": 1,
            "type": "stream.opened",
            "payload": {"status": "open"},
        },
        {
            **identity,
            "event_id": "evt_sse_block_started",
            "event_seq": 2,
            "type": "block.started",
            "payload": {
                "block_id": "block_1",
                "block_index": 0,
                "carrier_type": "reasoning",
            },
        },
    ]

    async def event_records() -> AsyncIterator[Mapping[str, object]]:
        for event in events:
            yield event

    stream = _stream_message_sse(
        event_records(), request=_Request(), heartbeat_interval_seconds=1
    )

    frames: list[str] = []
    for _ in range(2):
        frames.append(await asyncio.wait_for(anext(stream), timeout=0.5))

    assert frames[0].startswith("id: 1\nevent: stream.opened\ndata: ")
    assert frames[1].startswith("id: 2\nevent: block.started\ndata: ")
    assert all(frame.endswith("\n\n") for frame in frames)
    assert all(": heartbeat" not in frame for frame in frames)
    await stream.aclose()


@pytest.mark.asyncio
async def test_message_stream_sse_stops_heartbeat_after_disconnect() -> None:
    """客户端断开时不再发心跳并清理记录流。"""
    records = _IdleRecords()
    stream = _stream_message_sse(
        records,
        request=_Request(disconnected=True),
        heartbeat_interval_seconds=0.01,
    )

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=0.5)

    assert records.closed is True
