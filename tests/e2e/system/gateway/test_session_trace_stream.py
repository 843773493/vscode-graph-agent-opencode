from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.schemas.event import JobStartedEvent
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import e2e_port_block_for_file


@pytest.mark.asyncio
async def test_gateway_preserves_trace_cursor_and_relays_idle_heartbeat(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    gateway_port = e2e_port_block_for_file(Path(request.node.fspath)).port(20)
    gateway = start_gateway_process(
        workspace_root=workspace_root,
        default_backend_url="managed-by-gateway",
        port=gateway_port,
    )

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=httpx.Timeout(25.0, read=20.0),
        ) as client:
            await acquire_gateway_guest(client)
            create_response = await client.post(
                "/api/v1/sessions",
                json={"title": "SSE 心跳 E2E"},
            )
            assert create_response.status_code == 200, create_response.text
            session_id = create_response.json()["data"]["session_id"]

            event_id = "evt_gateway_sse_cursor"
            trace_store = TraceEventStore(
                sessions_dir=workspace_root / ".boxteam" / "sessions"
            )
            for index in range(2):
                await trace_store.append(
                    session_id,
                    JobStartedEvent(
                        event_id=f"evt_gateway_page_{index}",
                        job_id="job_gateway_sse_cursor",
                        agent_id="default",
                        timestamp=datetime.now(UTC),
                    ),
                )
            await trace_store.append(
                session_id,
                JobStartedEvent(
                    event_id=event_id,
                    job_id="job_gateway_sse_cursor",
                    agent_id="default",
                    timestamp=datetime.now(UTC),
                ),
            )

            trace_response = await client.get(
                f"/api/v1/sessions/{session_id}/traces",
                params={"limit": 1},
            )
            assert trace_response.status_code == 200, trace_response.text
            trace_page = trace_response.json()["data"]
            assert [event["event_id"] for event in trace_page["items"]] == [event_id]
            assert trace_page["has_more"] is True
            assert trace_page["next_cursor"].startswith("tp1.")

            older_response = await client.get(
                f"/api/v1/sessions/{session_id}/traces",
                params={"limit": 1, "cursor": trace_page["next_cursor"]},
            )
            assert older_response.status_code == 200, older_response.text
            assert [
                event["event_id"] for event in older_response.json()["data"]["items"]
            ] == ["evt_gateway_page_1"]

            stale_page_response = await client.get(
                f"/api/v1/sessions/{session_id}/traces",
                params={"cursor": "not-a-trace-page-cursor"},
            )
            assert stale_page_response.status_code == 410

            stale_cursor_response = await client.get(
                f"/api/v1/sessions/{session_id}/traces/stream",
                headers={"Last-Event-ID": "evt_missing_cursor"},
            )
            assert stale_cursor_response.status_code == 410
            stale_detail = stale_cursor_response.json()["detail"]
            assert stale_detail["code"] == "trace_cursor_gone"
            assert stale_detail["requested_cursor"] == "evt_missing_cursor"

            started_at = time.monotonic()
            lines_before_heartbeat: list[str] = []
            heartbeat_received = False
            async with asyncio.timeout(20.0):
                async with client.stream(
                    "GET",
                    f"/api/v1/sessions/{session_id}/traces/stream",
                    headers={"Last-Event-ID": event_id},
                ) as stream_response:
                    assert stream_response.status_code == 200
                    assert stream_response.headers["cache-control"] == "no-cache"
                    assert stream_response.headers["x-accel-buffering"] == "no"
                    assert stream_response.headers[
                        "x-boxteam-route-revision"
                    ].startswith("gw_")
                    assert stream_response.headers["x-request-id"]
                    async for line in stream_response.aiter_lines():
                        if not line:
                            continue
                        if line == ": heartbeat":
                            heartbeat_received = True
                            break
                        lines_before_heartbeat.append(line)

            assert heartbeat_received is True
            assert time.monotonic() - started_at < 20.0
            assert lines_before_heartbeat == [], (
                "Last-Event-ID 之后不应重放旧 Trace: "
                f"{lines_before_heartbeat}"
            )
    finally:
        close_gateway_process(gateway)
