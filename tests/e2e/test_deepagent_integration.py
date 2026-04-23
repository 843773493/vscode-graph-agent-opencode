#!/usr/bin/env python3
"""
真实DeepAgent端到端测试
使用真实KILO API密钥进行实际LLM调用测试
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncGenerator

import httpx
import pytest

from app.core.background_task_registry import BackgroundTaskRegistry
from app.main import app


@pytest.fixture
async def client(workspace_root_path: str) -> AsyncGenerator[httpx.AsyncClient, None]:
    print(f"使用测试工作区: {workspace_root_path}")
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            timeout=30,
            headers={"X-Local-Token": "local-dev-token"},
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_real_deepagent(client: httpx.AsyncClient, workspace_root_path: str):
    print("\n=== 测试真实DeepAgent端到端执行 ===")

    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "DeepAgent Integration Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]
    print(f"Session created: {session_id}")

    first_message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": "你好，请简单介绍一下你自己。",
            },
            "run": {
                "mode": "single_agent",
                "agent_id": "deep_agent",
            },
        },
    )
    assert first_message_response.status_code == 200
    first_job_id = first_message_response.json()["data"]["job_id"]
    print(f"First job started: {first_job_id}")

    first_result = await _wait_for_job_completion(client, first_job_id)
    assert first_result["status"] in {"completed", "succeeded"}
    assert not first_result.get("error_message")

    second_message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": "我刚才问了你什么问题？",
            },
            "run": {
                "mode": "single_agent",
                "agent_id": "deep_agent",
            },
        },
    )
    assert second_message_response.status_code == 200
    second_job_id = second_message_response.json()["data"]["job_id"]
    print(f"Second job started: {second_job_id}")

    second_result = await _wait_for_job_completion(client, second_job_id)
    assert second_result["status"] in {"completed", "succeeded"}
    assert not second_result.get("error_message")

    third_message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": (
                    "请在测试工作区根目录创建 test_deepagent_integration.md，"
                    "并写入刚才的对话历史。内容至少包含第一次自我介绍、"
                    "第二次问答，以及这次写文件的要求。"
                ),
            },
            "run": {
                "mode": "single_agent",
                "agent_id": "deep_agent",
            },
        },
    )
    assert third_message_response.status_code == 200
    third_job_id = third_message_response.json()["data"]["job_id"]
    print(f"Third job started: {third_job_id}")

    third_result = await _wait_for_job_completion(client, third_job_id)
    assert third_result["status"] in {"completed", "succeeded"}
    assert not third_result.get("error_message")

    workspace_root = Path(workspace_root_path)
    generated_file = workspace_root / "test_deepagent_integration.md"
    assert generated_file.exists(), f"未找到生成文件: {generated_file}"

    file_content = generated_file.read_text(encoding="utf-8")
    assert file_content.strip(), "生成的 test_deepagent_integration.md 为空"
    assert "第一次自我介绍" in file_content or "你好，请简单介绍一下你自己。" in file_content
    assert "我刚才问了你什么问题？" in file_content
    print(f"Generated file verified: {generated_file}")

    workspace_response = await client.get("/api/v1/workspace")
    assert workspace_response.status_code == 200
    assert workspace_response.json()["data"]["root_path"] == str(workspace_root)

    print("\n🎉 真实DeepAgent端到端测试通过！")


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

    result = await _wait_for_job_completion(client, job_id)
    assert result["status"] in {"completed", "succeeded"}
    assert not result.get("error_message")

    workspace_root = Path(workspace_root_path)
    trace_file = workspace_root / ".boxteam" / "logs" / "traces" / f"trace_{session_id}.jsonl"
    assert trace_file.exists(), f"未找到 trace 文件: {trace_file}"

    trace_lines = trace_file.read_text(encoding="utf-8").splitlines()
    trace_events = [json.loads(line) for line in trace_lines if line.strip()]
    python_tool_events = [
        event
        for event in trace_events
        if event.get("event_type") == "tool_call_start"
        and event.get("data", {}).get("tool_name") == "python_exec"
    ]

    assert python_tool_events, f"未在 trace 中观察到 python_exec 工具调用: {trace_file}"

    tool_output_events = [
        event
        for event in trace_events
        if event.get("event_type") == "tool_call_end"
        and event.get("data", {}).get("tool_name") == "python_exec"
    ]
    assert tool_output_events, f"未在 trace 中观察到 python_exec 工具调用结束事件: {trace_file}"
    assert any(
        "python tool ok" in event.get("data", {}).get("result", "")
        for event in tool_output_events
    ), f"python_exec 的执行结果中未包含预期输出: {trace_file}"

    print("✅ DeepAgent 已成功调用 python_exec 工具")


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
        event.get("event_type") == "tool_call_end"
        and event.get("data", {}).get("tool_name") == "collect_background_messages"
        and session2_first_final_text in event.get("data", {}).get("result", "")
        for event in session1_trace_events
    )

    assert repeated_in_session1_reply or repeated_in_tool_chain, (
        "session 1 未在回复或工具链中保留 session 2 的第一次 final_text"
    )

    assert any(
        event.get("event_type") == "tool_call_start"
        and event.get("data", {}).get("tool_name") == "monitor_session_agent_end"
        for event in session1_trace_events
    ), f"未在 trace 中观察到 monitor_session_agent_end 工具调用: {session1_trace_file}"
    assert any(
        event.get("event_type") == "tool_call_start"
        and event.get("data", {}).get("tool_name") == "collect_background_messages"
        for event in session1_trace_events
    ), f"未在 trace 中观察到 collect_background_messages 工具调用: {session1_trace_file}"
    assert any(
        event.get("event_type") == "tool_call_end"
        and event.get("data", {}).get("tool_name") == "collect_background_messages"
        for event in session1_trace_events
    ), f"未在 trace 中观察到 collect_background_messages 工具结束事件: {session1_trace_file}"
    assert any(
        event.get("event_type") == "tool_call_start"
        and event.get("data", {}).get("tool_name") == "send_message_to_session"
        for event in session1_trace_events
    ), f"未在 trace 中观察到 send_message_to_session 工具调用: {session1_trace_file}"
    assert any(
        event.get("event_type") == "tool_call_end"
        and event.get("data", {}).get("tool_name") == "send_message_to_session"
        for event in session1_trace_events
    ), f"未在 trace 中观察到 send_message_to_session 工具结束事件: {session1_trace_file}"

    print("✅ DeepAgent 跨 session final_text 监控测试通过")


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
        if event.get("event_type") == "tool_call_start"
        and event.get("data", {}).get("tool_name") == "python_exec"
    ]
    assert tool_starts, f"中断测试中未观察到 python_exec 工具调用: {trace_file}"

    print("✅ DeepAgent 中断和恢复测试通过")


async def _wait_for_job_completion(
    client: httpx.AsyncClient,
    job_id: str,
    *,
    trace_file: Path | None = None,
    session_id: str | None = None,
) -> dict:
    for attempt in range(30):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job_data = response.json()["data"]
        print(f"Job status (attempt {attempt + 1}): {job_data['status']}")

        if job_data["status"] in {"completed", "succeeded", "failed"}:
            if job_data["status"] == "failed":
                pytest.fail(f"Job failed: {job_data['error_message']}")
            return job_data

        await asyncio.sleep(1)

    message = f"Job timed out after 30 seconds: {job_id}"
    if session_id is not None and trace_file is not None:
        message = f"{message}\n{_collect_monitor_timeout_debug(session_id, job_id, trace_file)}"
    raise TimeoutError(message)


async def _wait_for_job_state(client: httpx.AsyncClient, job_id: str, expected_states: set[str]) -> dict:
    for attempt in range(60):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job_data = response.json()["data"]
        print(f"Job state (attempt {attempt + 1}): {job_data['status']}")

        if job_data["status"] in expected_states:
            return job_data

        if job_data["status"] == "failed":
            pytest.fail(f"Job failed while waiting for {expected_states}: {job_data['error_message']}")

        await asyncio.sleep(1)

    pytest.fail(f"Job timed out waiting for states {expected_states}: {job_id}")


async def _wait_for_trace_event(trace_file: Path, event_type: str, tool_name: str | None = None) -> dict:
    for attempt in range(60):
        if trace_file.exists():
            trace_events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            for event in trace_events:
                if event.get("event_type") != event_type:
                    continue
                if tool_name is not None and event.get("data", {}).get("tool_name") != tool_name:
                    continue
                return event

        print(f"Trace event wait (attempt {attempt + 1}): {trace_file}")
        await asyncio.sleep(1)

    pytest.fail(f"Job trace timed out waiting for {event_type} / {tool_name}: {trace_file}")


def _collect_monitor_timeout_debug(session_id: str, job_id: str, trace_file: Path) -> str:
    debug_lines = [f"session_id={session_id}", f"job_id={job_id}", f"trace_file={trace_file}"]

    handles = BackgroundTaskRegistry.get_instance().list_handles(session_id)
    if not handles:
        debug_lines.append("background_tasks=[]")
    else:
        debug_lines.append("background_tasks=")
        for handle in handles:
            debug_lines.append(
                f"  - task_id={handle.task_id} name={handle.task_name} status={handle.status} "
                f"created_at={handle.created_at.isoformat()} started_at={handle.started_at.isoformat() if handle.started_at else None} "
                f"ended_at={handle.ended_at.isoformat() if handle.ended_at else None} metadata={handle.metadata}"
            )

    if trace_file.exists():
        trace_lines = trace_file.read_text(encoding="utf-8").splitlines()
        tail = trace_lines[-20:]
        debug_lines.append("trace_tail=")
        debug_lines.extend(f"  {line}" for line in tail)
    else:
        debug_lines.append("trace_tail=<missing>")

    return "\n".join(debug_lines)


async def _wait_for_session_assistant_messages(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    min_count: int,
    timeout_seconds: int = 60,
) -> list[dict]:
    for attempt in range(timeout_seconds):
        response = await client.get(f"/api/v1/sessions/{session_id}/messages")
        assert response.status_code == 200
        items = response.json()["data"]["items"]
        assistant_messages = [message for message in items if message["role"] == "assistant"]
        if len(assistant_messages) >= min_count:
            return assistant_messages

        print(
            f"Session {session_id} assistant message wait (attempt {attempt + 1}): "
            f"{len(assistant_messages)}/{min_count}"
        )
        await asyncio.sleep(1)

    pytest.fail(
        f"Session {session_id} did not reach {min_count} assistant messages within {timeout_seconds} seconds"
    )
    
    
    
    
    
    