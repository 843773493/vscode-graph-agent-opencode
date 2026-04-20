import asyncio
import httpx
import pytest
import json
from typing import AsyncGenerator

from tests.conftest import use_config
from app.main import app

use_config("default")


@pytest.fixture
async def client(workspace_root_path: str) -> AsyncGenerator[httpx.AsyncClient, None]:
    print(f"使用测试工作区: {workspace_root_path}")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        timeout=30,
        headers={"X-Local-Token": "local-dev-token"},
    ) as client:
        yield client


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
    events = []

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
    max_attempts = 30
    for attempt in range(max_attempts):
        job_status_response = await client.get(f"/api/v1/jobs/{job_id}")
        assert job_status_response.status_code == 200
        job_status = job_status_response.json()["data"]

        print(f"Job status (attempt {attempt+1}): {job_status['status']}")

        if job_status["status"] in {"completed", "succeeded"}:
            print("Job completed successfully!")
            break
        elif job_status["status"] == "failed":
            pytest.fail(f"Job failed: {job_status['error_message']}")

        await asyncio.sleep(1)
    else:
        pytest.fail("Job timed out after 30 seconds")

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
async def test_multiple_parallel_jobs(client: httpx.AsyncClient):
    """测试多个Job并行运行"""
    
    # 创建会话
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Parallel Jobs Test"}
    )
    session_id = create_session_response.json()["data"]["session_id"]
    
    # 同时启动3个Job
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
        print(f"Started parallel job {i}: {job_ids[i]}")
    
    # 等待所有Job完成
    completed = 0
    for attempt in range(40):
        for job_id in job_ids:
            response = await client.get(f"/api/v1/jobs/{job_id}")
            status = response.json()["data"]["status"]
            if status in ["completed", "succeeded", "failed"]:
                completed += 1
        
        if completed == len(job_ids):
            break
        
        completed = 0
        await asyncio.sleep(1)
    else:
        pytest.fail("Not all jobs completed in time")
    
    print(f"✅ All {len(job_ids)} parallel jobs completed successfully!")


if __name__ == "__main__":
    asyncio.run(test_full_session_flow())
