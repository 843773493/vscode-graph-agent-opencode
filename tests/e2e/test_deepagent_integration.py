#!/usr/bin/env python3
"""DeepAgent 端到端测试。"""
from __future__ import annotations

import json
import asyncio
import time
from pathlib import Path

import httpx
import pytest

from app.main import app

from tests.e2e.utils import wait_for_job_done


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

    first_result = await wait_for_job_done(client, first_job_id)
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

    second_result = await wait_for_job_done(client, second_job_id)
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

    third_result = await wait_for_job_done(client, third_job_id)
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


def _collect_monitor_timeout_debug(session_id: str, job_id: str, trace_file: Path) -> str:
    debug_lines = [f"session_id={session_id}", f"job_id={job_id}", f"trace_file={trace_file}"]

    container = getattr(app.state, "container", None)
    if container is None:
        debug_lines.append("background_tasks=<container unavailable>")
        handles = []
    else:
        handles = container.background_task_registry.list_handles(session_id)
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
    
    
    
    
    
    