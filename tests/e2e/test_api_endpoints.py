import asyncio
import httpx
import pytest
import json
from typing import AsyncGenerator


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=30) as client:
        yield client


@pytest.mark.asyncio
async def test_full_session_flow(client: httpx.AsyncClient):
    """测试完整端到端流程：创建会话 → 发送消息 → 订阅SSE流 → 接收响应"""
    
    # 1. 创建会话
    create_session_response = await client.post(
        "/api/sessions",
        json={"name": "E2E Test Session", "metadata": {"test": True}}
    )
    assert create_session_response.status_code == 200
    session_data = create_session_response.json()
    session_id = session_data["session_id"]
    print(f"Created session: {session_id}")
    
    # 2. 启动异步Job
    start_job_response = await client.post(
        f"/api/sessions/{session_id}/jobs",
        json={"message": "Hello, please respond with a simple greeting."}
    )
    assert start_job_response.status_code == 200
    job_data = start_job_response.json()
    job_id = job_data["job_id"]
    print(f"Started job: {job_id}")
    
    # 3. 轮询Job状态直到完成
    max_attempts = 30
    for attempt in range(max_attempts):
        job_status_response = await client.get(f"/api/jobs/{job_id}")
        assert job_status_response.status_code == 200
        job_status = job_status_response.json()
        
        print(f"Job status (attempt {attempt+1}): {job_status['status']}")
        
        if job_status["status"] == "completed":
            print("Job completed successfully!")
            break
        elif job_status["status"] == "failed":
            pytest.fail(f"Job failed: {job_status['error_message']}")
        
        await asyncio.sleep(1)
    else:
        pytest.fail("Job timed out after 30 seconds")
    
    # 4. 测试SSE事件流
    print("Testing SSE event stream...")
    events = []
    async with client.stream("GET", f"/api/sessions/{session_id}/events") as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    events.append(event)
                    print(f"Received event: {event['type']}")
                except json.JSONDecodeError:
                    pass
    
    assert len(events) > 0, "No events received from SSE stream"
    print(f"Received {len(events)} events successfully")
    
    # 5. 验证会话消息历史
    messages_response = await client.get(f"/api/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()
    assert len(messages) >= 2, "Expected at least user and assistant messages"
    print(f"Session has {len(messages)} messages")
    
    print("✅ Full end-to-end flow completed successfully!")


@pytest.mark.asyncio
async def test_multiple_parallel_jobs(client: httpx.AsyncClient):
    """测试多个Job并行运行"""
    
    # 创建会话
    create_session_response = await client.post(
        "/api/sessions",
        json={"name": "Parallel Jobs Test"}
    )
    session_id = create_session_response.json()["session_id"]
    
    # 同时启动3个Job
    job_ids = []
    for i in range(3):
        response = await client.post(
            f"/api/sessions/{session_id}/jobs",
            json={"message": f"Hello from job {i}"}
        )
        job_ids.append(response.json()["job_id"])
        print(f"Started parallel job {i}: {job_ids[i]}")
    
    # 等待所有Job完成
    completed = 0
    for attempt in range(40):
        for job_id in job_ids:
            response = await client.get(f"/api/jobs/{job_id}")
            status = response.json()["status"]
            if status in ["completed", "failed"]:
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
