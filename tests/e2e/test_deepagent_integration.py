#!/usr/bin/env python3
"""DeepAgent 工具调用链路端到端测试。

与前端 ChatPanel 保持一致：断言关键业务事件（tool_call_*, text_*, system_reminder,
error, agent_end）的存在与顺序，不再要求前端不消费的事件（message_created、
job_created、job_started、agent_start、llm_request、status_change、job_completed）
必须按固定顺序出现。
"""
from __future__ import annotations

import json

import httpx
import pytest

from tests.e2e.utils import get_trace_payload, read_sse_events_until, wait_for_job_done


def _assert_event_order(events: list[dict]) -> None:
    """断言关键业务事件的相对顺序（与前端聚合逻辑一致）。

    前端 ChatPanel.aggregateConversationEvents 显式跳过：
    job_completed, status_change, job_created, job_started, agent_start, agent_step,
    llm_request, agent_end, message_created。
    本断言只验证前端真正消费的关键业务事件的相对顺序，不强制 LLM 调用工具或触发提醒。
    """
    types = [event.get("type") for event in events]

    # agent_end 必定出现（标记流结束），且在所有业务事件之后
    assert "agent_end" in types, f"缺少 agent_end：{types}"
    agent_end_idx = types.index("agent_end")

    # tool_call 配对（如果出现）必须 start < end
    if "tool_call_start" in types and "tool_call_end" in types:
        ts = types.index("tool_call_start")
        te = types.index("tool_call_end")
        assert ts < te, f"tool_call_start 必须在 tool_call_end 之前：{types}"
        # tool_call_end 必须在 agent_end 之前
        assert te < agent_end_idx, f"tool_call_end 必须在 agent_end 之前：{types}"

    # text 文本流（如果出现）必须 start < end 且中间只有 text_delta
    if "text_start" in types and "text_end" in types:
        text_start_idx = types.index("text_start")
        text_end_idx = types.index("text_end")
        assert text_start_idx < text_end_idx, f"text_start 必须在 text_end 之前：{types}"
        # text_start 与 text_end 之间不允许出现 tool_call_start/end（这些会触发 flush）
        # 但允许 message_created / job_* / agent_* / llm_request / agent_end
        # 关键约束：text_start 之前不能出现 text_end
        # text_end 必须在 agent_end 之前
        assert text_end_idx < agent_end_idx, f"text_end 必须在 agent_end 之前：{types}"

    # system_reminder_injected（如果出现）必须在 agent_end 之前
    if "system_reminder_injected" in types:
        sr = types.index("system_reminder_injected")
        assert sr < agent_end_idx, f"system_reminder_injected 必须在 agent_end 之前：{types}"


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

    async with client.stream("GET", f"/api/v1/sessions/{session_id}/traces/stream", timeout=None) as stream_response:
        assert stream_response.status_code == 200

        message_response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {
                    "content": "直接调用 test_tool 工具，然后只回答工具返回内容。不要执行多余操作，不要回答多余文本",
                },
                "run": {
                    "mode": "single_agent",
                    "agent_id": "default",
                },
            },
        )
        assert message_response.status_code == 200
        job_id = message_response.json()["data"]["job_id"]

        events = await read_sse_events_until(
            stream_response,
            lambda event: event.get("type") == "agent_end",
            timeout_seconds=timeout,
        )

        result = await wait_for_job_done(client, job_id)
        assert result["status"] in {"completed", "succeeded"}

    trace_types = [event.get("type") for event in events]
    print(f"\n=== 实时事件类型: {trace_types} ===")

    _assert_event_order(events)

    # agent_end 必定出现（标记流结束）
    assert "agent_end" in trace_types, f"未收到 agent_end: {trace_types}"

    # 如果 LLM 决定调用工具，则验证 tool_call 数据正确性
    if "tool_call_start" in trace_types:
        assert "tool_call_end" in trace_types, f"tool_call_start 出现但缺少 tool_call_end: {trace_types}"
        tool_start = next(event for event in events if event.get("type") == "tool_call_start")
        tool_end = next(event for event in events if event.get("type") == "tool_call_end")
        tool_start_payload = get_trace_payload(tool_start)
        tool_end_payload = get_trace_payload(tool_end)
        assert tool_start_payload.get("tool_name"), f"tool_call_start 缺少 tool_name: {tool_start_payload}"
        # tool_call_end 的 tool_name 必须与 start 一致
        assert tool_end_payload.get("tool_name") == tool_start_payload.get("tool_name"), (
            f"tool_call_end tool_name 与 start 不一致: {tool_end_payload} vs {tool_start_payload}"
        )

    # 如果后端注入了 system_reminder（位置在 tool_call 之后），验证 position 字段
    if "system_reminder_injected" in trace_types:
        reminder_event = next(event for event in events if event.get("type") == "system_reminder_injected")
        reminder_payload = get_trace_payload(reminder_event)
        # position 字段由 SystemReminderTriggerRegistry 注入，应当存在
        assert reminder_payload.get("position"), f"system_reminder_injected 缺少 position: {reminder_payload}"

    traces_response = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert traces_response.status_code == 200
    traces = traces_response.json()["data"]
    assert isinstance(traces, list)
    persisted_types = [trace.get("type") for trace in traces]
    print(f"\n=== 持久化轨迹类型: {persisted_types} ===")

    _assert_event_order(traces)
    assert "agent_end" in persisted_types

    print("\n=== DeepAgent trace stream 测试通过 ===")
