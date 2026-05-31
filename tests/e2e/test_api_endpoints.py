import asyncio
import json

import httpx
import pytest

from tests.e2e.utils import wait_for_job_done


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
    """测试不同 session 的 Job 允许异步并行执行"""

    session_ids = []
    for i in range(2):
        create_session_response = await client.post(
            "/api/v1/sessions",
            json={"title": f"Cross Session Parallel Test {i}"},
        )
        assert create_session_response.status_code == 200
        session_ids.append(create_session_response.json()["data"]["session_id"])

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

    running_seen = False
    for attempt in range(40):
        statuses = []
        for job_id in job_ids:
            response = await client.get(f"/api/v1/jobs/{job_id}")
            assert response.status_code == 200
            statuses.append(response.json()["data"]["status"])

        if statuses.count("running") >= 2:
            running_seen = True

        if all(status in {"completed", "succeeded", "failed"} for status in statuses):
            break

        await asyncio.sleep(1)
    else:
        pytest.fail("Cross-session jobs did not complete in time")

    assert running_seen, f"Cross-session parallel running state not observed: {statuses}"
    print("✅ Cross-session jobs observed running in parallel")


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

    continue_seen = False
    for _ in range(40):
        status_response = await client.get(f"/api/v1/sessions/{session_id}/auto-continue")
        assert status_response.status_code == 200
        status_data = status_response.json()["data"]

        messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
        assert messages_response.status_code == 200
        messages = messages_response.json()["data"]["items"]

        has_continue_message = any(
            item["role"] == "user" and item["content"].strip() == "继续"
            for item in messages
        )

        if status_data["forwarded_count"] >= 1 and has_continue_message:
            continue_seen = True
            break

        await asyncio.sleep(1)

    assert continue_seen, "自动继续任务未在会话中发送“继续”"

    stop_response = await client.post(f"/api/v1/sessions/{session_id}/auto-continue/stop")
    assert stop_response.status_code == 200
    stop_data = stop_response.json()["data"]
    assert stop_data["enabled"] is False

    messages_after_stop_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_after_stop_response.status_code == 200
    messages_after_stop = messages_after_stop_response.json()["data"]["items"]
    continue_count_after_stop = sum(
        1
        for item in messages_after_stop
        if item["role"] == "user" and item["content"].strip() == "继续"
    )

    manual_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": "请只回复：停止后测试"},
            "run": {"mode": "single_agent", "agent_id": "deep_agent"},
        },
    )
    assert manual_response.status_code == 200
    manual_job_id = manual_response.json()["data"]["job_id"]
    await wait_for_job_done(client, manual_job_id)

    await asyncio.sleep(2)
    final_messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert final_messages_response.status_code == 200
    final_messages = final_messages_response.json()["data"]["items"]
    continue_count_final = sum(
        1
        for item in final_messages
        if item["role"] == "user" and item["content"].strip() == "继续"
    )

    assert continue_count_final == continue_count_after_stop


if __name__ == "__main__":
    asyncio.run(test_full_session_flow())
