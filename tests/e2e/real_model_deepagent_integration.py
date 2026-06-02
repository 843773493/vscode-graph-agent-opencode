#!/usr/bin/env python3
"""真实 DeepAgent 端到端测试，仅用于手动真实模型验证。"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from tests.e2e.utils import wait_for_job_done


REAL_MODEL_SKIPIF = pytest.mark.skipif(
    not os.environ.get("OPENCODE_ZEN_API_KEY"),
    reason="缺少 OPENCODE_ZEN_API_KEY，跳过真实模型验证",
)


async def _wait_for_trace_event(
    trace_file: Path,
    *,
    event_type: str,
    tool_name: str,
    timeout_seconds: int = 60,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if trace_file.exists():
            trace_lines = trace_file.read_text(encoding="utf-8").splitlines()
            for line in trace_lines:
                if not line.strip():
                    continue
                event = json.loads(line)
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data", {})
                event_kind = event.get("type") or event.get("event_type")
                if event_kind == event_type and payload.get("tool_name") == tool_name:
                    return
        await asyncio.sleep(1)

    pytest.fail(f"在 trace 中未等到 {event_type}:{tool_name} 事件: {trace_file}")


async def _wait_for_session_assistant_messages(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    min_count: int,
    timeout_seconds: int = 60,
) -> list[dict]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/sessions/{session_id}/messages")
        assert response.status_code == 200
        messages = response.json()["data"]["items"]
        assistant_messages = [message for message in messages if message.get("role") == "assistant"]
        if len(assistant_messages) >= min_count:
            return assistant_messages
        await asyncio.sleep(1)

    pytest.fail(f"session {session_id} 未在超时时间内获得至少 {min_count} 条 assistant 消息")


@REAL_MODEL_SKIPIF
@pytest.mark.asyncio
async def test_real_deepagent_can_call_python_tool(client: httpx.AsyncClient, workspace_root_path: str):
    print("\n=== 测试真实DeepAgent调用 Python 工具 ===")

    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "DeepAgent Python Tool Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]
    print(f"Session created: {session_id}")

    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": (
                    "请务必调用 python_exec 工具执行以下代码：\n"
                    "print('python tool ok')\n"
                    "执行完成后只返回执行结果，不要直接猜答案。"
                ),
            },
            "run": {
                "mode": "single_agent",
                "agent_id": "deep_agent",
            },
        },
    )
    assert message_response.status_code == 200
    job_id = message_response.json()["data"]["job_id"]
    print(f"Job started: {job_id}")

    result = await wait_for_job_done(client, job_id)
    assert result["status"] in {"completed", "succeeded"}
    assert not result.get("error_message")

    workspace_root = Path(workspace_root_path)
    trace_file = workspace_root / ".boxteam" / "logs" / "traces" / f"trace_{session_id}.jsonl"
    assert trace_file.exists(), f"未找到 trace 文件: {trace_file}"

    trace_lines = trace_file.read_text(encoding="utf-8").splitlines()
    trace_events = [json.loads(line) for line in trace_lines if line.strip()]
    python_tool_events = []
    for event in trace_events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data", {})
        if event.get("type") == "tool_call_start" and payload.get("tool_name") == "python_exec":
            python_tool_events.append(event)

    assert python_tool_events, f"未在 trace 中观察到 python_exec 工具调用: {trace_file}"

    tool_output_events = []
    for event in trace_events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data", {})
        if event.get("type") == "tool_call_end" and payload.get("tool_name") == "python_exec":
            tool_output_events.append(event)
    assert tool_output_events, f"未在 trace 中观察到 python_exec 工具调用结束事件: {trace_file}"
    assert any(
        "python tool ok" in event.get("data", {}).get("result", "")
        for event in tool_output_events
    ), f"python_exec 的执行结果中未包含预期输出: {trace_file}"

    print("✅ DeepAgent 已成功调用 python_exec 工具")


@REAL_MODEL_SKIPIF
@pytest.mark.asyncio
async def test_real_deepagent_can_monitor_other_session_final_text_and_repeat(
    client: httpx.AsyncClient,
    workspace_root_path: str,
):
    print("\n=== 测试真实DeepAgent监控另一个 session 的 final_text ===")

    session1_response = await client.post(
        "/api/v1/sessions",
        json={"title": "DeepAgent Monitor Session Test - 1"},
    )
    assert session1_response.status_code == 200
    session1_id = session1_response.json()["data"]["session_id"]

    session2_response = await client.post(
        "/api/v1/sessions",
        json={"title": "DeepAgent Monitor Session Test - 2"},
    )
    assert session2_response.status_code == 200
    session2_id = session2_response.json()["data"]["session_id"]

    print(f"Session 1: {session1_id}")
    print(f"Session 2: {session2_id}")

    session1_message_response = await client.post(
        f"/api/v1/sessions/{session1_id}/messages",
        json={
            "message": {
                "content": (
                    "严格按顺序执行，禁止跳步："
                    f"1) 调用 monitor_session_agent_end 监控 session {session2_id}；"
                    "2) 紧接着调用 collect_background_messages，等待并读取 interrupt 消息；"
                    "3) 从 interrupt 消息里提取 final_text，并在你的回复中原样重复该 final_text；"
                    f"4) 调用 send_message_to_session，向 session {session2_id} 发送：再次只重复前面的话；"
                    "5) 完成后结束。"
                    "如果任一步未完成，不允许结束。"
                ),
            },
            "run": {
                "mode": "single_agent",
                "agent_id": "deep_agent",
            },
        },
    )
    assert session1_message_response.status_code == 200
    session1_job_id = session1_message_response.json()["data"]["job_id"]
    print(f"Session 1 job started: {session1_job_id}")

    workspace_root = Path(workspace_root_path)
    session1_trace_file = workspace_root / ".boxteam" / "logs" / "traces" / f"trace_{session1_id}.jsonl"

    await _wait_for_trace_event(
        session1_trace_file,
        event_type="tool_call_start",
        tool_name="monitor_session_agent_end",
    )

    await _wait_for_trace_event(
        session1_trace_file,
        event_type="tool_call_end",
        tool_name="monitor_session_agent_end",
    )

    session2_message_response = await client.post(
        f"/api/v1/sessions/{session2_id}/messages",
        json={
            "message": {
                "content": "请只回复：橙子",
            },
            "run": {
                "mode": "single_agent",
                "agent_id": "deep_agent",
            },
        },
    )
    assert session2_message_response.status_code == 200
    session2_job_id = session2_message_response.json()["data"]["job_id"]
    print(f"Session 2 job started: {session2_job_id}")

    session2_result = await _wait_for_job_completion(client, session2_job_id)
    assert session2_result["status"] in {"completed", "succeeded"}
    assert not session2_result.get("error_message")

    session2_messages_after_first = await _wait_for_session_assistant_messages(
        client,
        session2_id,
        min_count=1,
    )
    session2_first_final_text = session2_messages_after_first[-1]["content"].strip()

    try:
        session1_result = await _wait_for_job_completion(
            client,
            session1_job_id,
            trace_file=session1_trace_file,
            session_id=session1_id,
        )
    except TimeoutError as exc:
        debug_info = _collect_monitor_timeout_debug(session1_id, session1_job_id, session1_trace_file)
        pytest.fail(f"{exc}\n{debug_info}")

    assert session1_result["status"] in {"completed", "succeeded"}
    assert not session1_result.get("error_message")

    session1_messages_response = await client.get(f"/api/v1/sessions/{session1_id}/messages")
    assert session1_messages_response.status_code == 200
    session1_messages = session1_messages_response.json()["data"]["items"]
    session1_assistant_messages = [message for message in session1_messages if message["role"] == "assistant"]
    assert session1_assistant_messages, "session 1 没有生成助手回复"

    session2_messages_response = await client.get(f"/api/v1/sessions/{session2_id}/messages")
    assert session2_messages_response.status_code == 200
    session2_messages = session2_messages_response.json()["data"]["items"]
    session2_assistant_messages = [message for message in session2_messages if message["role"] == "assistant"]
    assert session2_assistant_messages, "session 2 没有生成助手回复"

    repeated_in_session1_reply = any(
        session2_first_final_text in message["content"]
        for message in session1_assistant_messages
    )

    session2_messages_after_second = await _wait_for_session_assistant_messages(
        client,
        session2_id,
        min_count=2,
    )
    session2_second_final_text = session2_messages_after_second[-1]["content"].strip()

    assert session2_second_final_text == session2_first_final_text

    session1_trace_events = [
        json.loads(line)
        for line in session1_trace_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    repeated_in_tool_chain = any(
        event.get("type") == "tool_call_end"
        and (event.get("payload") or event.get("data", {})).get("tool_name") == "collect_background_messages"
        and session2_first_final_text in ((event.get("payload") or event.get("data", {})).get("result", ""))
        for event in session1_trace_events
    )

    assert repeated_in_session1_reply or repeated_in_tool_chain, (
        "session 1 未在回复或工具链中保留 session 2 的第一次 final_text"
    )

    assert any(
        event.get("type") == "tool_call_start"
        and (event.get("payload") or event.get("data", {})).get("tool_name") == "monitor_session_agent_end"
        for event in session1_trace_events
    ), f"未在 trace 中观察到 monitor_session_agent_end 工具调用: {session1_trace_file}"
    assert any(
        event.get("type") == "tool_call_start"
        and (event.get("payload") or event.get("data", {})).get("tool_name") == "collect_background_messages"
        for event in session1_trace_events
    ), f"未在 trace 中观察到 collect_background_messages 工具调用: {session1_trace_file}"
    assert any(
        event.get("type") == "tool_call_end"
        and (event.get("payload") or event.get("data", {})).get("tool_name") == "collect_background_messages"
        for event in session1_trace_events
    ), f"未在 trace 中观察到 collect_background_messages 工具结束事件: {session1_trace_file}"
    assert any(
        event.get("type") == "tool_call_start"
        and (event.get("payload") or event.get("data", {})).get("tool_name") == "send_message_to_session"
        for event in session1_trace_events
    ), f"未在 trace 中观察到 send_message_to_session 工具调用: {session1_trace_file}"
    assert any(
        event.get("type") == "tool_call_end"
        and (event.get("payload") or event.get("data", {})).get("tool_name") == "send_message_to_session"
        for event in session1_trace_events
    ), f"未在 trace 中观察到 send_message_to_session 工具结束事件: {session1_trace_file}"

    print("✅ DeepAgent 跨 session final_text 监控测试通过")


@REAL_MODEL_SKIPIF
@pytest.mark.asyncio
async def test_real_deepagent_interrupt_and_resume(client: httpx.AsyncClient, workspace_root_path: str):
    print("\n=== 测试真实DeepAgent打断和恢复 ===")

    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "DeepAgent Interrupt Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]
    print(f"Session created: {session_id}")

    start_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": (
                    "请务必调用 python_exec 执行以下代码：\n"
                    "import time\n"
                    "time.sleep(5)\n"
                    "print('interrupt test done')\n"
                    "执行完后再给出一句简短总结，不要跳过工具调用。"
                ),
            },
            "run": {
                "mode": "single_agent",
                "agent_id": "deep_agent",
            },
        },
    )
    assert start_response.status_code == 200
    job_id = start_response.json()["data"]["job_id"]
    print(f"Interrupt test job started: {job_id}")

    workspace_root = Path(workspace_root_path)
    trace_file = workspace_root / ".boxteam" / "logs" / "traces" / f"trace_{session_id}.jsonl"
    await _wait_for_trace_event(
        trace_file,
        event_type="tool_call_start",
        tool_name="python_exec",
    )

    pause_response = await client.post(
        f"/api/v1/jobs/{job_id}/control",
        json={"action": "pause"},
    )
    assert pause_response.status_code == 200
    pause_data = pause_response.json()["data"]
    assert pause_data["status"] in {"interrupt_pending", "paused"}

    paused_job = await _wait_for_job_state(client, job_id, {"paused"})
    assert paused_job["status"] == "paused"

    resume_response = await client.post(
        f"/api/v1/jobs/{job_id}/control",
        json={
            "action": "resume",
            "input": {"decision": "continue"},
        },
    )
    assert resume_response.status_code == 200
    resume_data = resume_response.json()["data"]
    assert resume_data["status"] == "running"

    final_job = await _wait_for_job_completion(client, job_id)
    assert final_job["status"] in {"completed", "succeeded"}
    assert not final_job.get("error_message")

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()["data"]["items"]
    assistant_messages = [message for message in messages if message["role"] == "assistant"]
    assert assistant_messages, "恢复后的任务没有写回助手消息"

    assert trace_file.exists(), f"未找到 trace 文件: {trace_file}"

    trace_events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    tool_starts = [
        event for event in trace_events
        if event.get("type") == "tool_call_start"
        and (event.get("payload") or event.get("data", {})).get("tool_name") == "python_exec"
    ]
    assert tool_starts, f"中断测试中未观察到 python_exec 工具调用: {trace_file}"

    print("✅ DeepAgent 中断和恢复测试通过")
