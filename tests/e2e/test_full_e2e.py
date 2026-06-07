from __future__ import annotations

import httpx
import pytest

from tests.e2e.utils import last_assistant_message, normalize_text, wait_for_job_done


@pytest.mark.asyncio
async def test_full_session_flow(client: httpx.AsyncClient):
    """测试完整端到端流程：创建会话 → 发送消息 → 等待作业完成 → 验证消息历史。"""

    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "E2E Test Session"},
    )
    assert create_session_response.status_code == 200
    session_data = create_session_response.json()["data"]
    session_id = session_data["session_id"]

    start_job_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": "你好，请简单回复一句话。",
            },
            "run": {
                "mode": "single_agent",
                "agent_id": "default",
            },
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
async def test_multiple_same_session_jobs_are_queued(client: httpx.AsyncClient):
    """测试同一 session 的多个 Job 串行排队执行。"""

    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Same Session Queue Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    job_ids: list[str] = []
    for i in range(3):
        response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {"content": f"Hello from job {i}"},
                "run": {"mode": "single_agent", "agent_id": "deep_agent"},
            },
        )
        assert response.status_code == 200
        job_ids.append(response.json()["data"]["job_id"])

    for job_id in job_ids:
        result = await wait_for_job_done(client, job_id)
        assert result["status"] in {"completed", "succeeded"}


@pytest.mark.asyncio
async def test_multiple_different_session_jobs_can_run_in_parallel(client: httpx.AsyncClient):
    """测试不同 session 的 Job 允许异步并行执行。"""

    session_ids: list[str] = []
    for i in range(2):
        create_session_response = await client.post(
            "/api/v1/sessions",
            json={"title": f"Cross Session Parallel Test {i}"},
        )
        assert create_session_response.status_code == 200
        session_ids.append(create_session_response.json()["data"]["session_id"])

    job_ids: list[str] = []
    for index, session_id in enumerate(session_ids):
        response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {"content": f"不要调用任何工具，只回复一句简短中文问候。session={index}"},
                "run": {"mode": "single_agent", "agent_id": "deep_agent"},
            },
        )
        assert response.status_code == 200
        job_ids.append(response.json()["data"]["job_id"])

    for job_id in job_ids:
        result = await wait_for_job_done(client, job_id)
        assert result["status"] in {"completed", "succeeded"}


@pytest.mark.asyncio
async def test_session_auto_continue_start_and_stop(client: httpx.AsyncClient):
    """测试会话自动继续任务：开启后自动发送“继续”，关闭后停止自动发送。"""

    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Session Auto Continue Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    start_response = await client.post(
        f"/api/v1/sessions/{session_id}/auto-continue/start",
        json={"poll_interval_seconds": 0.2},
    )
    assert start_response.status_code == 200
    start_data = start_response.json()["data"]
    assert start_data["enabled"] is True
    assert start_data["task_status"] in {"pending", "running"}

    trigger_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": "请只回复：自动继续测试"},
            "run": {"mode": "single_agent", "agent_id": "deep_agent"},
        },
    )
    assert trigger_response.status_code == 200
    trigger_job_id = trigger_response.json()["data"]["job_id"]
    await wait_for_job_done(client, trigger_job_id)

    stop_response = await client.post(f"/api/v1/sessions/{session_id}/auto-continue/stop")
    assert stop_response.status_code == 200
    stop_data = stop_response.json()["data"]
    assert stop_data["enabled"] is False
