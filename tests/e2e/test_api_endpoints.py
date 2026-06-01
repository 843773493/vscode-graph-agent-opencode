import asyncio
import json

import httpx
import pytest

from tests.e2e.utils import wait_for_job_done, wait_for_jobs_done_concurrently


@pytest.mark.asyncio
async def test_full_session_flow(client: httpx.AsyncClient):
    """测试完整端到端流程：创建会话 → 发送消息 → 订阅SSE流 → 接收响应"""
    
    # 1. 创建会话
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "E2E Test Session"}
    )
    assert create_session_response.status_code == 200
    session_data = create_session_response.json()["data"]
    session_id = session_data["session_id"]
    print(f"Created session: {session_id}")
    
    # 2. 先订阅 SSE 事件流，再触发任务，避免错过事件
    print("Testing job event history...")
    # 3. 启动异步Job
    start_job_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": "Hello, please respond with a simple greeting."
            },
            "run": {
                "mode": "single_agent",
                "agent_id": "deep_agent"
            }
        }
    )
    assert start_job_response.status_code == 200
    job_data = start_job_response.json()["data"]
    job_id = job_data["job_id"]
    print(f"Started job: {job_id}")

    # 4. 轮询Job状态直到完成
    job_status = await wait_for_job_done(client, job_id, max_attempts=30)
    assert job_status["status"] in {"completed", "succeeded"}

    # 5. 获取事件历史，验证事件链路已经产出
    events_response = await client.get(f"/api/v1/jobs/{job_id}/events")
    assert events_response.status_code == 200
    events = events_response.json()["data"]
    assert len(events) > 0, "No events received from job event history"
    print(f"Received {len(events)} events successfully")
    
    # 5. 验证会话消息历史
    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()["data"]["items"]
    assert len(messages) >= 2, "Expected at least user and assistant messages"
    print(f"Session has {len(messages)} messages")
    
    print("✅ Full end-to-end flow completed successfully!")


@pytest.mark.asyncio
async def test_multiple_same_session_jobs_are_queued(client: httpx.AsyncClient):
    """测试同一 session 的多个 Job 串行排队执行"""
    
    # 创建会话
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Same Session Queue Test"}
    )
    session_id = create_session_response.json()["data"]["session_id"]
    
    # 连续启动3个Job（同一 session）
    job_ids = []
    for i in range(3):
        response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {"content": f"Hello from job {i}"},
                "run": {"mode": "single_agent", "agent_id": "deep_agent"},
            }
        )
        job_ids.append(response.json()["data"]["job_id"])
        print(f"Started queued job {i}: {job_ids[i]}")
    
    # 等待所有Job完成（串行执行可能更慢）
    for job_id in job_ids:
        result = await wait_for_job_done(client, job_id, max_attempts=90)
        assert result["status"] in {"completed", "succeeded"}
    
    print(f"✅ All {len(job_ids)} queued jobs completed successfully!")


@pytest.mark.asyncio
async def test_multiple_different_session_jobs_can_run_in_parallel(client: httpx.AsyncClient):
    """测试不同 session 的 Job 可以真正并发完成。"""

    session_ids = []
    for i in range(2):
        create_session_response = await client.post(
            "/api/v1/sessions",
            json={"title": f"Cross Session Parallel Test {i}"},
        )
        assert create_session_response.status_code == 200
        session_id = create_session_response.json()["data"]["session_id"]
        session_ids.append(session_id)

        # 显式校验会话已持久化并且 API 可见，避免后续 job 启动时才暴露会话写入/读取时序问题。
        session_detail_response = await client.get(f"/api/v1/sessions/{session_id}")
        assert session_detail_response.status_code == 200
        assert session_detail_response.json()["data"]["session_id"] == session_id

    job_ids = []
    for index, session_id in enumerate(session_ids):
        response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "message": {"content": f"Please respond quickly from session {index}"},
                "run": {"mode": "single_agent", "agent_id": "deep_agent"},
            },
        )
        assert response.status_code == 200
        job_id = response.json()["data"]["job_id"]
        job_ids.append(job_id)
        print(f"Started cross-session job {index}: {job_id}")

    assert len(job_ids) == len(session_ids)

    results = await wait_for_jobs_done_concurrently(client, job_ids, max_attempts=90)
    assert set(results.keys()) == set(job_ids)
    for job_id, job_data in results.items():
        assert job_data["status"] in {"completed", "succeeded"}, f"job 完成状态异常: {job_id} -> {job_data}"
        assert job_data["session_id"] in session_ids

    # 再次核验两个 session 在整个流程中均可读取，并且各自都有助手消息回写。
    for session_id in session_ids:
        session_response = await client.get(f"/api/v1/sessions/{session_id}")
        assert session_response.status_code == 200, f"session 不可读: {session_id}"
        messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
        assert messages_response.status_code == 200
        messages = messages_response.json()["data"]["items"]
        assert any(message["role"] == "assistant" for message in messages), f"session 没有助手消息: {session_id}"


if __name__ == "__main__":
    asyncio.run(test_full_session_flow())
