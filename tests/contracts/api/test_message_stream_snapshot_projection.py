"""SSE 控制帧与 HTTP 快照必须共用同一条公共投影。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_message_stream_store
from app.api.message_stream import _sse_frame
from app.api.message_stream import router as message_stream_router
from app.core.path_utils import get_session_path_resolver
from app.core.trace_middleware import TraceMiddleware
from app.protocol.codecs.message_stream import (
    message_stream_to_json,
    message_stream_to_proto,
)
from app.services.infrastructure import message_stream_store as store_module
from app.services.infrastructure.message_stream_store import MessageStreamStore
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

_IDENTITY_FIELDS = ("session_id", "turn_id", "turn_stream_id")


@pytest.fixture
def message_stream_api() -> tuple[FastAPI, MessageStreamStore, str, str]:
    output_root = (
        Path.cwd() / "out/tests/contracts/api/test_message_stream_snapshot_projection"
    )
    if output_root.exists():
        shutil.rmtree(output_root)
    sessions_root = output_root / "workspace" / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    resolver.initialize()
    session_id = "ses_12345678123446788234567812345678"
    turn_id = "job_snapshot_projection"
    seed_catalog_session_bundle(sessions_root, session_id)

    store = MessageStreamStore(path_resolver=resolver)
    api = FastAPI()
    api.add_middleware(TraceMiddleware)
    api.include_router(message_stream_router, prefix="/api/v1")
    api.dependency_overrides[get_message_stream_store] = lambda: store
    try:
        yield api, store, session_id, turn_id
    finally:
        api.dependency_overrides.clear()


def _headers(request_id: str) -> dict[str, str]:
    return {"X-Local-Token": "local-dev-token", "X-Request-ID": request_id}


def _frames(body: str) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    for block in body.strip().split("\n\n"):
        data_line = next(
            (
                line.removeprefix("data:")
                for line in block.splitlines()
                if line.startswith("data:")
            ),
            None,
        )
        if data_line is not None:
            value = json.loads(data_line)
            assert isinstance(value, dict)
            frames.append(value)
    return frames


@pytest.mark.asyncio
async def test_snapshot_control_frame_matches_http_snapshot_payload(
    message_stream_api: tuple[FastAPI, MessageStreamStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, store, session_id, turn_id = message_stream_api
    # 只有 block 增量：current_attempt 恒为 0，tool_calls/activities 等全为空数组。
    monkeypatch.setattr(store_module, "MESSAGE_STREAM_MAX_BYTES", 1_000)
    monkeypatch.setattr(store_module, "MESSAGE_STREAM_RETAINED_BYTES", 260)
    writer = await store.open(session_id=session_id, turn_id=turn_id)
    for index in range(12):
        await writer.commit(
            "block.delta",
            {
                "block_id": "block_1",
                "carrier_type": "text",
                "operation": "append",
                "text": f"增量-{index}",
            },
            block_id="block_1",
        )
    # 终态快照让订阅在推送控制帧后自然结束，同时覆盖 resumable=False。
    await writer.close_failed(
        code="execution_lost",
        message="后端重启",
        resumable=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://testserver",
    ) as client:
        # 游标早于保留范围，订阅必须改推 stream.snapshot 控制帧。
        stream_response = await client.get(
            f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream",
            headers=_headers("req_snapshot_control"),
        )
        snapshot_response = await client.get(
            f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream/snapshot",
            headers=_headers("req_snapshot_http"),
        )

    assert stream_response.status_code == 200
    assert snapshot_response.status_code == 200
    frames = _frames(stream_response.text)
    control = next(frame for frame in frames if frame["type"] == "stream.snapshot")
    assert control["type"] == "stream.snapshot"
    sse_payload = control["payload"]
    assert isinstance(sse_payload, dict)
    http_payload = snapshot_response.json()["data"]
    http_core = {
        key: value for key, value in http_payload.items() if key not in _IDENTITY_FIELDS
    }

    # 两条出口共用同一份公共投影：去掉 HTTP 信封替代字段后逐键一致。
    assert http_core == sse_payload
    assert control["event_seq"] == sse_payload["snapshot_seq"]
    # 定位字段只保留在事件信封中，SSE payload 不得重复承载。
    assert all(field not in sse_payload for field in _IDENTITY_FIELDS)
    # 零值与空 repeated 必须在控制帧里显式存在。
    assert sse_payload["current_attempt"] == 0
    assert sse_payload["resumable"] is False
    for field in (
        "blocks",
        "tool_calls",
        "tool_executions",
        "model_calls",
        "activities",
        "resource_refs",
    ):
        assert isinstance(sse_payload[field], list)
    assert [block.get("items") for block in sse_payload["blocks"]] == [[]]


@pytest.mark.asyncio
async def test_non_snapshot_event_wire_format_is_unchanged(
    message_stream_api: tuple[FastAPI, MessageStreamStore, str, str],
) -> None:
    _, store, session_id, turn_id = message_stream_api
    writer = await store.open(session_id=session_id, turn_id=turn_id)
    cases = [
        (
            "model.started",
            {"model_call_id": "mc_1", "attempt": 1, "model": "primary"},
            {"model_call_id": "mc_1"},
        ),
        (
            "block.started",
            {"block_id": "b1", "block_index": 0, "carrier_type": "text"},
            {"block_id": "b1"},
        ),
        (
            "block.delta",
            {
                "block_id": "b1",
                "carrier_type": "text",
                "operation": "append",
                "text": "x",
            },
            {"block_id": "b1"},
        ),
        (
            "block.completed",
            {
                "block_id": "b1",
                "block_index": 0,
                "carrier_type": "text",
                "status": "completed",
                "completion_reason": "upstream_completed",
            },
            {"block_id": "b1"},
        ),
        (
            "tool.started",
            {"tool_execution_id": "e1", "tool_call_id": "c1", "tool_name": "shell"},
            {"tool_execution_id": "e1"},
        ),
        (
            "tool.completed",
            {
                "tool_execution_id": "e1",
                "tool_call_id": "c1",
                "tool_name": "shell",
                "status": "completed",
                "outcome": "success",
            },
            {"tool_execution_id": "e1"},
        ),
        ("stream.completed", {"status": "completed"}, {}),
        (
            "stream.interrupted",
            {"interrupt_request_id": "i1", "status": "interrupted"},
            {},
        ),
        (
            "stream.failed",
            {
                "code": "execution_lost",
                "message": "后端重启",
                "after_interrupt_requested": False,
                "resumable": False,
            },
            {},
        ),
        (
            "interrupt.requested",
            {"interrupt_request_id": "i1", "reason": "user_requested"},
            {},
        ),
    ]

    for index, (event_type, payload, envelope) in enumerate(cases, start=1):
        event = {
            "event_id": f"evt_{index}",
            "session_id": session_id,
            "turn_id": turn_id,
            "turn_stream_id": writer.turn_stream_id,
            "event_seq": index,
            "type": event_type,
            "payload": payload,
            **envelope,
        }
        expected = message_stream_to_json(message_stream_to_proto(event))
        frame = _sse_frame(event)
        data_line = next(
            line.removeprefix("data:")
            for line in frame.splitlines()
            if line.startswith("data:")
        )
        actual = json.loads(data_line)

        assert actual == expected, event_type
        # 非快照事件不得被公共投影补出快照专属的空数组或零值标量。
        assert actual["payload"] == expected["payload"], event_type
