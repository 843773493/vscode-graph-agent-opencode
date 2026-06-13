#!/usr/bin/env python3
"""DeepAgent 工具调用链路端到端测试。"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.e2e.utils import wait_for_job_done


async def _read_sse_events_until(
    response: httpx.Response,
    predicate,
    timeout_seconds: float = 30.0,
) -> list[dict]:
    events: list[dict] = []
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    async for line in response.aiter_lines():
        if asyncio.get_running_loop().time() >= deadline:
            break

        line = line.strip()
        if not line or line.startswith(":") or line.startswith("event:"):
            continue

        if line.startswith("data:"):
            raw = line.removeprefix("data:").strip()
            if raw:
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue

        if events and predicate(events[-1]):
            break

    return events


@pytest.mark.asyncio
async def test_deepagent_trace_stream(client: httpx.AsyncClient, is_debug: bool):
    print("\n=== 测试 test_tool 工具调用链路 ===")

    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "DeepAgent Test Tool Trace Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    timeout = 100000 if is_debug else 60.0

    async with client.stream("GET", f"/api/v1/sessions/{session_id}/events/stream", timeout=None) as stream_response:
        assert stream_response.status_code == 200

        message_response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {
                    "content": "只允许执行一次 test_tool 调用。先调用 test_tool，再立刻结束。不要写文件，不要读文件，不要执行命令，不要调用其它工具，不要解释，不要总结。",
                },
                "run": {
                    "mode": "single_agent",
                    "agent_id": "default",
                },
            },
        )
        assert message_response.status_code == 200
        job_id = message_response.json()["data"]["job_id"]

        events = await _read_sse_events_until(
            stream_response,
            lambda event: event.get("type") == "agent_end",
            timeout_seconds=timeout,
        )

        result = await wait_for_job_done(client, job_id)
        assert result["status"] in {"completed", "succeeded"}

    trace_types = [event.get("type") for event in events]
    print(f"\n=== 实时事件类型: {trace_types} ===")

    assert "tool_call_start" in trace_types, f"未收到 tool_call_start: {trace_types}"
    assert "tool_call_end" in trace_types, f"未收到 tool_call_end: {trace_types}"
    assert "agent_end" in trace_types, f"未收到 agent_end: {trace_types}"

    tool_start = next(event for event in events if event.get("type") == "tool_call_start")
    tool_end = next(event for event in events if event.get("type") == "tool_call_end")
    agent_end = next(event for event in events if event.get("type") == "agent_end")

    assert tool_start["payload"]["tool_name"] == "test_tool"
    assert tool_end["payload"]["tool_name"] == "test_tool"
    assert "2333" in tool_end["payload"].get("result", "") or "2333" in json.dumps(tool_end["payload"], ensure_ascii=False)

    traces_response = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert traces_response.status_code == 200
    traces = traces_response.json()["data"]
    assert isinstance(traces, list)
    persisted_types = [trace.get("type") for trace in traces]
    print(f"\n=== 持久化轨迹类型: {persisted_types} ===")

    assert "tool_call_start" in persisted_types
    assert "tool_call_end" in persisted_types
    assert "agent_end" in persisted_types

    print("\n test_tool 工具调用链路测试通过！")
