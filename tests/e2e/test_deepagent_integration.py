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
y

def _assert_event_order(events: list[dict]) -> None:
    """断言事件序列符合 DeepAgent 单次工具调用 + 可选文本生成的合理顺序。"""
    types = [event.get("type") for event in events]

    required_order = [
        "message_created",
        "job_created",
        "job_started",
        "agent_start",
        "llm_request",
        "tool_call_start",
        "tool_call_end",
        "agent_end",
    ]

    cursor = 0
    for expected in required_order:
        try:
            idx = types.index(expected, cursor)
        except ValueError:
            raise AssertionError(
                f"事件顺序错误：未在合适位置找到 '{expected}'。当前事件序列：{types}"
            )
        cursor = idx + 1

    if "job_completed" in types:
        assert types.index("agent_end") < types.index("job_completed"), (
            f"agent_end 必须在 job_completed 之前：{types}"
        )

    if "text_start" in types:
        text_start_idx = types.index("text_start")
        text_end_idx = types.index("text_end")
        agent_end_idx = types.index("agent_end")
        llm_indices = [i for i, t in enumerate(types) if t == "llm_request"]
        assert len(llm_indices) >= 2, f"存在文本输出时需要至少两次 llm_request：{types}"
        second_llm_idx = llm_indices[1]
        assert second_llm_idx < text_start_idx < text_end_idx < agent_end_idx, (
            f"文本事件位置错误：需要在第二次 llm_request 之后、agent_end 之前。序列：{types}"
        )

        for j in range(text_start_idx + 1, text_end_idx):
            if types[j] != "text_delta":
                raise AssertionError(
                    f"事件顺序错误：text_start 与 text_end 之间出现非 text_delta 事件 '{types[j]}'。"
                    f"序列：{types}"
                )

    tool_start_idx = types.index("tool_call_start")
    tool_end_idx = types.index("tool_call_end")
    agent_start_idx = types.index("agent_start")
    agent_end_idx = types.index("agent_end")

    assert tool_start_idx < tool_end_idx, f"tool_call_start 必须在 tool_call_end 之前：{types}"
    assert agent_start_idx < agent_end_idx, f"agent_start 必须在 agent_end 之前：{types}"

    if "system_reminder_injected" in types:
        sri_idx = types.index("system_reminder_injected")
        assert tool_end_idx < sri_idx < agent_end_idx, (
            f"system_reminder_injected 必须在 tool_call_end 之后、agent_end 之前：{types}"
        )

    if "job_completed" in types:
        assert agent_end_idx < types.index("job_completed"), f"agent_end 必须在 job_completed 之前：{types}"


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

    _assert_event_order(events)

    assert "tool_call_start" in trace_types, f"未收到 tool_call_start: {trace_types}"
    assert "tool_call_end" in trace_types, f"未收到 tool_call_end: {trace_types}"
    assert "agent_end" in trace_types, f"未收到 agent_end: {trace_types}"
    assert "system_reminder_injected" in trace_types, f"未收到 system_reminder_injected: {trace_types}"

    reminder_event = next(event for event in events if event.get("type") == "system_reminder_injected")
    assert reminder_event["payload"]["position"] == "after_tool_calls"
    assert "test_tool" in reminder_event["payload"]["content"]

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

    _assert_event_order(traces)

    assert "tool_call_start" in persisted_types
    assert "tool_call_end" in persisted_types
    assert "agent_end" in persisted_types
    assert "system_reminder_injected" in persisted_types

    print("\n test_tool 工具调用链路测试通过！")
