#!/usr/bin/env python3
"""Session 用户打断端到端测试。"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest


async def _wait_for_job_terminal(
    client: httpx.AsyncClient,
    job_id: str,
    max_attempts: int = 60,
) -> dict:
    for _ in range(max_attempts):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        data = response.json()["data"]
        status = data["status"]

        if status in {"completed", "succeeded", "failed", "cancelled", "timed_out"}:
            return data

        await asyncio.sleep(1)

    pytest.fail(f"Job {job_id} 超时未到达终态")


async def _run_interrupt_then_second_message(
    client: httpx.AsyncClient,
    session_id: str,
    first_message_content: str,
    target_event_type: str,
    second_message_content: str,
    timeout_seconds: float = 60.0,
) -> tuple[str, list[dict]]:
    """在第一个消息流中按事件打断，然后发送第二条消息。"""
    events: list[dict] = []
    interrupted = False
    first_job_id: str | None = None

    async with client.stream(
        "GET", f"/api/v1/sessions/{session_id}/traces/stream", timeout=None
    ) as stream_response:
        assert stream_response.status_code == 200

        message_response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {"content": first_message_content},
                "run": {"mode": "single_agent", "agent_id": "default"},
            },
        )
        assert message_response.status_code == 200
        first_job_id = message_response.json()["data"]["job_id"]

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        async for line in stream_response.aiter_lines():
            if asyncio.get_running_loop().time() >= deadline:
                break

            line = line.strip()
            if not line or line.startswith(":") or line.startswith("event:"):
                continue

            if line.startswith("data:"):
                raw = line.removeprefix("data:").strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                events.append(event)

                if not interrupted and event.get("type") == target_event_type:
                    interrupted = True
                    interrupt_response = await client.post(
                        f"/api/v1/sessions/{session_id}/interrupt"
                    )
                    assert interrupt_response.status_code == 200
                    interrupt_data = interrupt_response.json()["data"]
                    assert interrupt_data["job_id"] == first_job_id
                    assert interrupt_data["phase"] in {"text", "tool"}

                if event.get("type") == "job_cancelled":
                    break

    assert first_job_id is not None

    first_job_data = await _wait_for_job_terminal(client, first_job_id)
    assert first_job_data["status"] in {"failed", "cancelled"}

    second_message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": second_message_content},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert second_message_response.status_code == 200
    second_job_id = second_message_response.json()["data"]["job_id"]

    second_job_data = await _wait_for_job_terminal(client, second_job_id)
    assert second_job_data["status"] in {"completed", "succeeded"}

    return first_job_id, events


@pytest.mark.asyncio
async def test_interrupt_text_phase_then_send_second_message(client: httpx.AsyncClient):
    """在 text 生成阶段打断，然后发送第二条简单消息，验证 history 与 trace 符合预期。"""
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Interrupt Text Phase Then Second Message Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    first_job_id, events = await _run_interrupt_then_second_message(
        client,
        session_id,
        first_message_content="请用中文详细介绍如何学习 Python 编程，从基础语法到实践项目，至少 800 字，不要调用任何工具。",
        target_event_type="text_delta",
        second_message_content="简单回复一句话：继续测试",
    )

    event_types = [event.get("type") for event in events]
    assert "text_delta" in event_types, f"未收到 text_delta 事件: {event_types}"
    assert "session_interrupted" in event_types, f"未收到 session_interrupted 事件: {event_types}"
    assert "job_cancelled" in event_types, f"未收到 job_cancelled 事件: {event_types}"

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()["data"]["items"]

    roles = [msg.get("role") for msg in messages]
    assert roles == ["user", "assistant", "user", "assistant"], f"消息 role 序列不符合预期: {roles}"

    first_assistant = messages[1]
    assert "<system_reminder>" in first_assistant["content"]
    assert "文本生成" in first_assistant["content"]
    assert first_assistant["metadata"].get("phase") == "text"

    second_assistant = messages[3]
    assert "<system_reminder>" not in second_assistant["content"]
    assert "继续测试" in second_assistant["content"]


@pytest.mark.asyncio
async def test_interrupt_tool_phase_then_send_second_message(client: httpx.AsyncClient):
    """在 tool 调用阶段打断，然后发送第二条简单消息，验证 history 与 trace 符合预期。"""
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Interrupt Tool Phase Then Second Message Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    first_job_id, events = await _run_interrupt_then_second_message(
        client,
        session_id,
        first_message_content="调用 test_tool 工具，然后不要做任何其他事。",
        target_event_type="tool_call_start",
        second_message_content="忽略我之前的请求，不要调用任何工具，直接简单回复一句话：工具已取消。",
    )

    event_types = [event.get("type") for event in events]
    assert "tool_call_start" in event_types, f"未收到 tool_call_start 事件: {event_types}"
    assert "session_interrupted" in event_types, f"未收到 session_interrupted 事件: {event_types}"
    assert "job_cancelled" in event_types, f"未收到 job_cancelled 事件: {event_types}"

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()["data"]["items"]

    # 工具阶段：被打断后被打断的 assistant 消息由服务层注入 <system_reminder>，
    # messages 序列为 4 条：user -> assistant(system_reminder+tool) -> user -> assistant
    roles = [msg.get("role") for msg in messages]
    assert roles == ["user", "assistant", "user", "assistant"], f"消息 role 序列不符合预期: {roles}"

    first_assistant = messages[1]
    assert "<system_reminder>" in first_assistant["content"]
    assert "test_tool" in first_assistant["content"]
    assert first_assistant["metadata"].get("phase") == "tool"

    second_assistant = messages[3]
    assert "<system_reminder>" not in second_assistant["content"]
    assert "工具已取消" in second_assistant["content"]
