#!/usr/bin/env python3
"""Job 调度端到端测试。"""
from __future__ import annotations

import httpx
import pytest

from tests.e2e.utils import wait_for_job_done, wait_for_jobs_done_concurrently


@pytest.mark.asyncio
async def test_multiple_same_session_jobs_are_queued(client: httpx.AsyncClient):
    """同一 session 的多个 Job 串行排队执行。"""
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Same Session Queue Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    job_ids: list[str] = []
    for i in range(3):
        response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {"content": f"Hello from job {i}"},
                "run": {"mode": "single_agent", "agent_id": "default"},
            },
        )
        assert response.status_code == 200
        job_ids.append(response.json()["data"]["job_id"])
        print(f"Started queued job {i}: {job_ids[i]}")

    for job_id in job_ids:
        result = await wait_for_job_done(client, job_id, max_attempts=90)
        assert result["status"] in {"completed", "succeeded"}

    print(f"All {len(job_ids)} queued jobs completed successfully!")


@pytest.mark.asyncio
async def test_multiple_different_session_jobs_can_run_in_parallel(client: httpx.AsyncClient):
    """不同 session 的 Job 可以并发完成。"""
    session_ids: list[str] = []
    for i in range(2):
        create_session_response = await client.post(
            "/api/v1/sessions",
            json={"title": f"Cross Session Parallel Test {i}"},
        )
        assert create_session_response.status_code == 200
        session_id = create_session_response.json()["data"]["session_id"]
        session_ids.append(session_id)

        session_detail_response = await client.get(f"/api/v1/sessions/{session_id}")
        assert session_detail_response.status_code == 200
        assert session_detail_response.json()["data"]["session_id"] == session_id

    job_ids: list[str] = []
    for index, session_id in enumerate(session_ids):
        response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {
                    "content": f"不要调用任何工具，只回复一句简短中文问候。session={index}"
                },
                "run": {"mode": "single_agent", "agent_id": "default"},
            },
        )
        assert response.status_code == 200
        job_id = response.json()["data"]["job_id"]
        job_ids.append(job_id)
        print(f"Started cross-session job {index}: {job_id}")

    assert len(job_ids) == len(session_ids)

    results = await wait_for_jobs_done_concurrently(client, job_ids, max_attempts=90)
    assert set(results.keys()) == set(job_ids)
    for job_id, job_data in results.items():
        assert job_data["status"] in {"completed", "succeeded"}, f"job 完成状态异常: {job_id} -> {job_data}"
        assert job_data["session_id"] in session_ids

    for session_id in session_ids:
        session_response = await client.get(f"/api/v1/sessions/{session_id}")
        assert session_response.status_code == 200, f"session 不可读: {session_id}"
        messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
        assert messages_response.status_code == 200
        messages = messages_response.json()["data"]["items"]
        assert any(message["role"] == "assistant" for message in messages), f"session 没有助手消息: {session_id}"


@pytest.mark.asyncio
async def test_job_event_history_is_available(client: httpx.AsyncClient):
    """Job 完成后可以查询其事件历史。"""
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Job Event History Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    start_job_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": "Hello, please respond with a simple greeting."},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert start_job_response.status_code == 200
    job_id = start_job_response.json()["data"]["job_id"]

    received_live_event = False
    async with client.stream(
        "GET",
        f"/api/v1/jobs/{job_id}/events/stream",
        timeout=30,
    ) as stream_response:
        assert stream_response.status_code == 200
        async for line in stream_response.aiter_lines():
            if line.startswith("data: "):
                received_live_event = True
                break
    assert received_live_event, "Job SSE 未收到实时事件"

    job_status = await wait_for_job_done(client, job_id, max_attempts=30)
    assert job_status["status"] in {"completed", "succeeded"}

    events_response = await client.get(f"/api/v1/jobs/{job_id}/events")
    assert events_response.status_code == 200
    events = events_response.json()["data"]
    assert len(events) > 0, "No events received from job event history"
    traces_response = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert traces_response.status_code == 200
    traces = traces_response.json()["data"]
    trace_event_ids = [trace["event_id"] for trace in traces]
    assert {event["event_id"] for event in events}.issubset(set(trace_event_ids))

    cursor_index = max(0, len(traces) - 3)
    cursor = traces[cursor_index]["event_id"]
    replay_response = await client.get(
        f"/api/v1/sessions/{session_id}/traces",
        params={"after_event_id": cursor},
    )
    assert replay_response.status_code == 200
    replayed_ids = [trace["event_id"] for trace in replay_response.json()["data"]]
    assert replayed_ids == trace_event_ids[cursor_index + 1 :]
    print(f"Received {len(events)} events successfully")
