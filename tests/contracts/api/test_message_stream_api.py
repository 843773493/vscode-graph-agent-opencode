from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_message_stream_store
from app.api.message_stream import router as message_stream_router
from app.core.session_paths import SessionPathResolver
from app.core.trace_middleware import TraceMiddleware
from app.services.infrastructure.message_stream_store import MessageStreamStore


@pytest.fixture
def message_stream_api() -> tuple[FastAPI, MessageStreamStore, str, str]:
    output_root = (
        Path.cwd() / "out/tests/contracts/api/test_message_stream_api"
    )
    if output_root.exists():
        shutil.rmtree(output_root)
    sessions_root = output_root / "workspace" / ".boxteam" / "sessions"
    resolver = SessionPathResolver(sessions_root)
    resolver.initialize()
    session_id = "ses_message_stream_api"
    turn_id = "job_message_stream_api"
    session_dir = resolver.allocate_session_dir(
        session_id=session_id,
        title=session_id,
    )
    now = datetime.now(UTC).isoformat()
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "title": session_id,
                "created_at": now,
                "updated_at": now,
            }
        ),
        encoding="utf-8",
    )
    resolver.register_session(session_id, session_dir)

    store = MessageStreamStore(path_resolver=resolver)
    api = FastAPI()
    api.add_middleware(TraceMiddleware)
    api.include_router(message_stream_router, prefix="/api/v1")
    api.dependency_overrides[get_message_stream_store] = lambda: store
    try:
        yield api, store, session_id, turn_id
    finally:
        api.dependency_overrides.clear()


def _headers(request_id: str = "req_message_stream_api") -> dict[str, str]:
    return {
        "X-Local-Token": "local-dev-token",
        "X-Request-ID": request_id,
    }


def _sse_events(body: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for frame in body.strip().split("\n\n"):
        data_line = next(
            (line.removeprefix("data:") for line in frame.splitlines() if line.startswith("data:")),
            None,
        )
        if data_line is not None:
            value = json.loads(data_line)
            assert isinstance(value, dict)
            events.append(value)
    return events


@pytest.mark.asyncio
async def test_message_stream_sse_replay_and_terminal_close(
    message_stream_api: tuple[FastAPI, MessageStreamStore, str, str],
) -> None:
    api, store, session_id, turn_id = message_stream_api
    writer = await store.open(session_id=session_id, turn_id=turn_id)
    await writer.commit(
        "block.started",
        {
            "block_id": "block_1",
            "block_index": 0,
            "carrier_type": "reasoning",
        },
    )
    await writer.commit(
        "block.delta",
        {
            "block_id": "block_1",
            "operation": "append",
            "text": "已持久化",
        },
    )
    await writer.commit(
        "block.completed",
        {
            "block_id": "block_1",
            "block_index": 0,
            "carrier_type": "reasoning",
            "status": "completed",
            "completion_reason": "upstream_completed",
        },
    )
    await writer.close_completed()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream",
            headers=_headers("req_sse_replay"),
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req_sse_replay"
    assert response.headers["x-message-stream-id"] == writer.turn_stream_id
    events = _sse_events(response.text)
    assert [event["event_seq"] for event in events] == [1, 2, 3, 4, 5]
    assert events[-1]["type"] == "stream.completed"


@pytest.mark.asyncio
async def test_message_stream_snapshot_replaces_state_and_preserves_request_id(
    message_stream_api: tuple[FastAPI, MessageStreamStore, str, str],
) -> None:
    api, store, session_id, turn_id = message_stream_api
    writer = await store.open(session_id=session_id, turn_id=turn_id)
    await writer.commit(
        "model.started",
        {"model_call_id": "model_1", "attempt": 1, "model": "primary"},
        model_call_id="model_1",
    )
    await writer.commit(
        "block.started",
        {
            "block_id": "block_1",
            "block_index": 0,
            "carrier_type": "text",
        },
        model_call_id="model_1",
        block_id="block_1",
    )
    await writer.commit(
        "block.delta",
        {
            "block_id": "block_1",
            "block_index": 0,
            "carrier_type": "text",
            "operation": "append",
            "text": "当前答案",
        },
        model_call_id="model_1",
        block_id="block_1",
    )
    await writer.commit(
        "tool_call",
        {
            "tool_call_id": "call_1",
            "tool_name": "shell",
            "arguments": {"command": "pwd"},
            "status": "streaming",
        },
    )
    await writer.commit(
        "tool.started",
        {
            "tool_execution_id": "exec_1",
            "tool_call_id": "call_1",
            "tool_name": "shell",
        },
        tool_execution_id="exec_1",
    )
    await writer.commit(
        "model.failed",
        {
            "model_call_id": "model_1",
            "attempt": 1,
            "outcome": "upstream_error",
            "error_code": "provider_error",
            "message": "上游失败详情",
        },
        model_call_id="model_1",
    )
    await writer.close_failed(
        code="execution_lost",
        message="后端重启",
        resumable=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream/snapshot",
            headers=_headers("req_snapshot"),
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req_snapshot"
    body = response.json()
    assert body["request_id"] == "req_snapshot"
    snapshot = body["data"]
    assert snapshot["turn_stream_id"] == writer.turn_stream_id
    assert snapshot["stream_status"] == "failed"
    assert snapshot["failure"]["code"] == "execution_lost"
    assert snapshot["blocks"][0]["text"] == "当前答案"
    assert "model_call_id" not in snapshot["blocks"][0]
    assert snapshot["model_calls"][0]["outcome"] == "upstream_error"
    assert "error_code" not in snapshot["model_calls"][0]
    assert "message" not in snapshot["model_calls"][0]
    assert snapshot["tool_calls"][0]["arguments"] == {"command": "pwd"}
    assert snapshot["tool_executions"][0]["status"] == "completed"
    assert snapshot["tool_executions"][0]["outcome"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_failed_legacy_snapshot_drops_internal_tool_call_error_without_500(
    message_stream_api: tuple[FastAPI, MessageStreamStore, str, str],
) -> None:
    api, store, session_id, turn_id = message_stream_api
    writer = await store.open(session_id=session_id, turn_id=turn_id)
    await writer.commit(
        "tool_call",
        {
            "tool_call_id": "call_legacy",
            "tool_name": "read_file",
            "arguments": {"path": "legacy.txt"},
            "status": "incomplete",
            "completion_reason": "agent_event_timeout",
            "error": "内部错误详情不属于公共 ToolCall schema",
        },
    )
    await writer.close_failed(
        code="agent_event_timeout",
        message="Agent 事件流等待模型响应超过 60 秒",
        resumable=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream/snapshot",
            headers=_headers("req_legacy_snapshot"),
        )

    assert response.status_code == 200
    snapshot = response.json()["data"]
    assert snapshot["stream_status"] == "failed"
    assert snapshot["failure"]["code"] == "agent_event_timeout"
    assert snapshot["tool_calls"][0]["tool_call_id"] == "call_legacy"
    assert "error" not in snapshot["tool_calls"][0]


@pytest.mark.asyncio
async def test_active_snapshot_projects_model_failure_without_500(
    message_stream_api: tuple[FastAPI, MessageStreamStore, str, str],
) -> None:
    api, store, session_id, turn_id = message_stream_api
    writer = await store.open(session_id=session_id, turn_id=turn_id)
    await writer.commit(
        "model.failed",
        {
            "model_call_id": "model_active_failure",
            "attempt": 9,
            "outcome": "upstream_error",
            "error_code": "ScopeCancelledError",
            "message": "运行时 scope 已取消: reason=scope_deadline_exceeded",
            "retryable": True,
        },
        model_call_id="model_active_failure",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream/snapshot",
            headers=_headers("req_active_model_failure"),
        )

    assert response.status_code == 200
    snapshot = response.json()["data"]
    assert snapshot["stream_status"] == "open"
    assert snapshot["failure"]["code"] == "ScopeCancelledError"
    assert snapshot["failure"]["message"].startswith("运行时 scope 已取消")
    assert "model_call_id" not in snapshot["failure"]
    assert snapshot["model_calls"][0]["outcome"] == "upstream_error"


@pytest.mark.asyncio
async def test_message_stream_availability_does_not_create_missing_streams(
    message_stream_api: tuple[FastAPI, MessageStreamStore, str, str],
) -> None:
    api, store, session_id, turn_id = message_stream_api
    writer = await store.open(session_id=session_id, turn_id=turn_id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            f"/api/v1/sessions/{session_id}/message-streams/availability",
            params=[("turn_ids", turn_id), ("turn_ids", "job_without_stream")],
            headers=_headers("req_availability"),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "req_availability"
    assert body["data"] == {turn_id: writer.turn_stream_id}


@pytest.mark.asyncio
async def test_message_stream_replay_after_cursor_and_unknown_stream_error(
    message_stream_api: tuple[FastAPI, MessageStreamStore, str, str],
) -> None:
    api, store, session_id, turn_id = message_stream_api
    writer = await store.open(session_id=session_id, turn_id=turn_id)
    await writer.commit(
        "interrupt.requested",
        {"interrupt_request_id": "intr_api", "reason": "user_requested"},
    )
    await writer.close_interrupted("intr_api")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://testserver",
    ) as client:
        replay = await client.get(
            f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream/events",
            params={"after_seq": 1},
            headers=_headers("req_replay"),
        )
        unknown = await client.get(
            f"/api/v1/sessions/{session_id}/turns/{turn_id}/message-stream",
            params={"turn_stream_id": "strm_does_not_exist"},
            headers=_headers("req_unknown"),
        )
        missing_snapshot = await client.get(
            f"/api/v1/sessions/{session_id}/turns/job_without_stream/message-stream/snapshot",
            headers=_headers("req_missing_snapshot"),
        )

    assert replay.status_code == 200
    replay_body = replay.json()
    assert replay_body["request_id"] == "req_replay"
    assert [event["event_seq"] for event in replay_body["data"]] == [2, 3]
    assert replay_body["data"][-1]["type"] == "stream.interrupted"
    assert unknown.status_code == 404
    assert missing_snapshot.status_code == 404
