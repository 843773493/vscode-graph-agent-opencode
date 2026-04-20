#!/usr/bin/env python3
"""
真实DeepAgent端到端测试
使用真实KILO API密钥进行实际LLM调用测试
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncGenerator

import httpx
import pytest

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


async def _wait_for_job_completion(client: httpx.AsyncClient, job_id: str) -> dict:
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

    pytest.fail(f"Job timed out after 30 seconds: {job_id}")