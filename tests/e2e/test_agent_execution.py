#!/usr/bin/env python3
"""Agent 执行端到端测试。

注：SSE 事件顺序断言、payload 提取等通用工具已迁移到 tests/e2e/utils.py。
trace 事件流测试已迁移到 tests/e2e/test_deepagent_integration.py。
"""
from __future__ import annotations

import httpx
import pytest

from tests.e2e.utils import wait_for_job_done


@pytest.mark.asyncio
async def test_agent_initialization(client: httpx.AsyncClient):
    """验证后端可通过 HTTP 正常创建会话并接受消息。"""
    response = await client.post(
        "/api/v1/sessions",
        json={"title": "Agent Initialization Test"},
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
async def test_single_step_execution(client: httpx.AsyncClient):
    """最小单步执行测试。"""
    response = await client.post(
        "/api/v1/sessions",
        json={"title": "Single Step Execution Test"},
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
    assert job_data["session_id"] == session_id


@pytest.mark.asyncio
async def test_session_isolation(client: httpx.AsyncClient):
    """不同 session 具有隔离状态。"""
    create_a_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Session A Isolation Test"},
    )
    assert create_a_response.status_code == 200
    session_a = create_a_response.json()["data"]["session_id"]

    create_b_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Session B Isolation Test"},
    )
    assert create_b_response.status_code == 200
    session_b = create_b_response.json()["data"]["session_id"]

    job_a1_response = await client.post(
        f"/api/v1/sessions/{session_a}/messages",
        json={
            "message": {"content": "记住这个数字：42"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert job_a1_response.status_code == 200
    job_a1 = job_a1_response.json()["data"]["job_id"]

    job_b1_response = await client.post(
        f"/api/v1/sessions/{session_b}/messages",
        json={
            "message": {"content": "记住这个数字：88"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert job_b1_response.status_code == 200
    job_b1 = job_b1_response.json()["data"]["job_id"]

    job_a2_response = await client.post(
        f"/api/v1/sessions/{session_a}/messages",
        json={
            "message": {"content": "我刚才告诉你的数字是什么？"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert job_a2_response.status_code == 200
    job_a2 = job_a2_response.json()["data"]["job_id"]

    job_b2_response = await client.post(
        f"/api/v1/sessions/{session_b}/messages",
        json={
            "message": {"content": "我刚才告诉你的数字是什么？"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert job_b2_response.status_code == 200
    job_b2 = job_b2_response.json()["data"]["job_id"]

    resp_a1 = await wait_for_job_done(client, job_a1)
    resp_b1 = await wait_for_job_done(client, job_b1)
    resp_a2 = await wait_for_job_done(client, job_a2)
    resp_b2 = await wait_for_job_done(client, job_b2)

    assert all(response["status"] in {"completed", "succeeded"} for response in [resp_a1, resp_b1, resp_a2, resp_b2])

    session_a_messages = await client.get(f"/api/v1/sessions/{session_a}/messages")
    session_b_messages = await client.get(f"/api/v1/sessions/{session_b}/messages")
    assert session_a_messages.status_code == 200
    assert session_b_messages.status_code == 200
    assert session_a_messages.json()["data"]["items"]
    assert session_b_messages.json()["data"]["items"]
