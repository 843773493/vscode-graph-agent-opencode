#!/usr/bin/env python3
"""Agent 执行端到端测试。

注：SSE 事件顺序断言、payload 提取等通用工具已迁移到 tests/e2e/utils.py。
trace 事件流测试已迁移到 tests/e2e/test_deepagent_integration.py。
"""
from __future__ import annotations

import httpx
import pytest
from pathlib import Path

from tests.e2e.utils import get_trace_payload, wait_for_job_done


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
async def test_llm_request_log_round_trip(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    """真实 HTTP 闭环验证模型调用日志可落盘并通过 API 读回。"""
    Path(e2e_workspace_root_path, "AGENTS.md").write_text(
        "# LLM 日志 E2E\n\n请保留此规则用于验证 Prompt 来源回放。\n",
        encoding="utf-8",
    )
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "LLM Request Logging E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    prompt = "请只回复 LOG_OK，不要解释。"
    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": prompt},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200
    job_id = message_response.json()["data"]["job_id"]

    job_data = await wait_for_job_done(client, job_id)
    assert job_data["status"] in {"completed", "succeeded"}

    logs_response = await client.get(
        f"/api/v1/sessions/{session_id}/llm-request-logs"
    )
    assert logs_response.status_code == 200
    logs = logs_response.json()["data"]
    matching_logs = [log for log in logs if log.get("job_id") == job_id]
    assert matching_logs

    log = matching_logs[-1]
    request = log["request"]
    response = log["response"]
    assert any(
        message.get("content") == prompt
        for message in request["messages"]
        if isinstance(message, dict)
    )
    assert isinstance(request.get("tools"), list)
    assert request["tools"]
    replay = request["replay"]
    assert replay["schema_version"] == 1
    assert replay["message_count"] == len(request["messages"])
    assert replay["system_prompt_char_count"] > 0
    assert replay["tools"]["count"] == len(request["tools"])
    assert replay["tools"]["schema_char_count"] > 0
    assert replay["prompt_components"]
    assert replay["prompt_components"][0]["label"] == "默认指令"
    assert "工作区 AGENTS.md" in {
        component["label"] for component in replay["prompt_components"]
    }
    assert all(
        component["block_count"] > 0 and component["char_count"] > 0
        for component in replay["prompt_components"]
    )
    assert isinstance(response.get("result"), list)
    assert response["result"]

    state_response = await client.get(
        f"/api/v1/sessions/{session_id}/agent-state/messages"
    )
    assert state_response.status_code == 200
    agent_state_jsonl = state_response.json()["data"]["jsonl"]
    assert "_llm_request_replay_trace" not in agent_state_jsonl
    assert "llm_request_prompt_replay_trace" not in agent_state_jsonl

    expected_log_dir = (
        Path(e2e_workspace_root_path).resolve()
        / ".boxteam"
        / "sessions"
        / session_id
        / "logs"
        / "llm_requests"
    )
    log_path = Path(log["file_path"]).resolve()
    assert log_path.is_relative_to(expected_log_dir)
    assert log_path.is_file()


@pytest.mark.asyncio
async def test_reply_token_usage_is_persisted_in_trace_and_message(
    client: httpx.AsyncClient,
):
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Reply Token Usage E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": "请只回复 TOKEN_USAGE_OK，不要调用工具。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert message_response.status_code == 200
    job_id = message_response.json()["data"]["job_id"]
    await wait_for_job_done(client, job_id, max_attempts=90)

    events_response = await client.get(f"/api/v1/jobs/{job_id}/events")
    assert events_response.status_code == 200
    agent_end = next(
        event
        for event in events_response.json()["data"]
        if event["type"] == "agent_end"
    )
    token_usage = agent_end["payload"]["token_usage"]
    assert token_usage["total_tokens"] > 0
    assert token_usage["reported_model_calls"] == token_usage["model_calls"]
    assert token_usage["model_calls"] >= 1
    assert isinstance(token_usage["cache_read_input_tokens"], int)

    traces_response = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert traces_response.status_code == 200
    persisted_agent_end = next(
        event
        for event in traces_response.json()["data"]
        if event["type"] == "agent_end" and event["job_id"] == job_id
    )
    assert get_trace_payload(persisted_agent_end)["token_usage"] == token_usage

    messages_response = await client.get(
        f"/api/v1/sessions/{session_id}/messages"
    )
    assert messages_response.status_code == 200
    assistant_message = next(
        message
        for message in reversed(messages_response.json()["data"]["items"])
        if message["role"] == "assistant"
    )
    assert assistant_message["metadata"]["token_usage"] == token_usage


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
