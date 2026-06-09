#!/usr/bin/env python3
"""DeepAgent 工具调用链路端到端测试。"""
from __future__ import annotations

import json

import httpx
import pytest

from tests.e2e.utils import wait_for_job_done


async def _read_sse_event(response: httpx.Response, expected_event: str, timeout_seconds: float = 10.0) -> dict:
    import asyncio

    buffer: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    async for chunk in response.aiter_text():
        for raw_line in chunk.splitlines():
            line = raw_line.strip()
            if not line:
                event_name = None
                data_line = None
                for item in buffer:
                    if item.startswith("event: "):
                        event_name = item.removeprefix("event: ").strip()
                    elif item.startswith("data: "):
                        data_line = item.removeprefix("data: ").strip()

                buffer = []
                if event_name == expected_event and data_line:
                    return json.loads(data_line)
                continue

            buffer.append(line)

        if asyncio.get_running_loop().time() >= deadline:
            break

    raise AssertionError(f"未在超时时间内收到 SSE 事件: {expected_event}")


@pytest.mark.asyncio
async def test_deepagent_trace_stream(client: httpx.AsyncClient):
    print("\n=== 测试 test_tool 工具调用链路 ===")

    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "DeepAgent Test Tool Trace Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    async with client.stream("GET", f"/api/v1/sessions/{session_id}/traces/stream") as stream_response:
        assert stream_response.status_code == 200
        ready_event = await _read_sse_event(stream_response, "trace")
        assert ready_event["type"] == "agent_start"
        assert ready_event["session_id"] == session_id
        assert ready_event["title"] == "轨迹流已连接"

        message_response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {
                    "content": "只允许执行一次 test_tool 调用。先调用 test_tool，再立刻结束。不要写文件，不要读文件，不要执行命令，不要调用其它工具，不要解释，不要总结。",
                },
                "run": {
                    "mode": "single_agent",
                    "agent_id": "deep_agent",
                },
            },
        )
        assert message_response.status_code == 200
        job_id = message_response.json()["data"]["job_id"]

        result = await wait_for_job_done(client, job_id)
        assert result["status"] in {"completed", "succeeded"}

    traces_response = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert traces_response.status_code == 200
    traces = traces_response.json()["data"]
    assert isinstance(traces, list)
    trace_events = [trace for trace in traces if trace["type"] in {"tool_call_start", "tool_call_end", "agent_end"}]
    assert [trace["type"] for trace in trace_events[:3]] == ["tool_call_start", "tool_call_end", "agent_end"]
    assert trace_events[0].get("tool_name") == "test_tool"
    assert trace_events[1].get("tool_name") == "test_tool"
    assert "2333" in trace_events[1].get("content", "") or "2333" in json.dumps(trace_events[1].get("raw", {}), ensure_ascii=False)
    assert "2333" in trace_events[2].get("content", "") or "2333" in json.dumps(trace_events[2].get("raw", {}), ensure_ascii=False)

    print("\n🎉 test_tool 工具调用链路测试通过！")
