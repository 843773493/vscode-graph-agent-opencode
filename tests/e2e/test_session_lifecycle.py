#!/usr/bin/env python3
"""Session 生命周期端到端测试。"""
from __future__ import annotations

import httpx
import pytest

from tests.e2e.utils import (
    last_assistant_message,
    normalize_text,
    wait_for_job_done,
)


@pytest.mark.asyncio
async def test_create_session_and_send_message(client: httpx.AsyncClient):
    """创建会话并发送消息，验证后端可正常接受请求。"""
    response = await client.post(
        "/api/v1/sessions",
        json={"title": "Session Lifecycle Test"},
    )
    assert response.status_code == 200
    session_id = response.json()["data"]["session_id"]

    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": "简单回复一句话：你好，我是测试助手"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200
    job_id = message_response.json()["data"]["job_id"]

    job_data = await wait_for_job_done(client, job_id)
    assert job_data["status"] in {"completed", "succeeded"}


@pytest.mark.asyncio
async def test_full_session_flow(client: httpx.AsyncClient):
    """完整 session 流：创建 → 发送消息 → 等待完成 → 验证消息历史。"""
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "E2E Test Session"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    start_job_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": "你好，请简单回复一句话。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert start_job_response.status_code == 200
    job_id = start_job_response.json()["data"]["job_id"]

    job_data = await wait_for_job_done(client, job_id)
    assert job_data["status"] in {"completed", "succeeded"}

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()["data"]["items"]
    assistant_reply = normalize_text(last_assistant_message(messages))

    assert assistant_reply
    assert len(messages) >= 2


@pytest.mark.asyncio
async def test_frontend_response_diagnostic_flow(client: httpx.AsyncClient):
    """诊断型 e2e：排查前端拿不到响应时后端链路状态。"""
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Frontend Response Diagnostic Session"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    message_content = "请只回复一句简短的中文问候。"
    start_job_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": message_content},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert start_job_response.status_code == 200
    accepted = start_job_response.json()["data"]
    message_id = accepted["message_id"]
    job_id = accepted["job_id"]

    assert accepted["status"] in {"accepted", "queued", "running"}
    assert message_id
    assert job_id

    job_data = await wait_for_job_done(client, job_id, max_attempts=90)
    assert job_data["job_id"] == job_id
    assert job_data["session_id"] == session_id
    assert job_data["status"] in {"completed", "succeeded"}

    events_response = await client.get(f"/api/v1/jobs/{job_id}/events")
    assert events_response.status_code == 200
    events = events_response.json()["data"]
    assert events, "job 事件为空，说明后端没有完整发布执行轨迹"

    agent_end_events = [event for event in events if event["type"] == "agent_end"]
    job_completed_events = [event for event in events if event["type"] == "job_completed"]
    assert agent_end_events, "缺少 agent_end 事件"
    assert job_completed_events, "缺少 job_completed 事件"

    agent_end_final_texts = [
        normalize_text(str(event.get("payload", {}).get("final_text", "")))
        for event in agent_end_events
    ]
    assert any(agent_end_final_texts), "agent_end 的 final_text 为空"
    assert len({text for text in agent_end_final_texts if text}) == 1, "多个 agent_end final_text 不一致"

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()["data"]["items"]
    assert len(messages) >= 2, "至少应该有 user 和 assistant 两条消息"

    assistant_reply = normalize_text(last_assistant_message(messages))
    assert assistant_reply, "assistant 回复为空"
    assert assistant_reply != normalize_text(message_content), "assistant 回复不应等于用户输入"
    assert assistant_reply == agent_end_final_texts[-1], "assistant 消息与 agent_end.final_text 不一致"

    created_user_message = next((msg for msg in messages if msg["message_id"] == message_id), None)
    assert created_user_message is not None, "找不到刚创建的用户消息"
    assert created_user_message["role"] == "user"
    assert normalize_text(created_user_message["content"]) == normalize_text(message_content)

    print("Diagnostic flow completed successfully: accepted -> job_done -> events -> assistant reply")
