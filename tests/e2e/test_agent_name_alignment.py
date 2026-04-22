from __future__ import annotations

import asyncio
import os
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


@pytest.fixture(autouse=True)
def setup_agent_name_config():
    from app.services.config_service import ConfigService, set_config_path

    ConfigService.reset_instance()
    config_path = Path(__file__).resolve().parents[2] / "configs" / "tests" / "agent_name_check.json"
    set_config_path(str(config_path))
    yield
    ConfigService.reset_instance()
    set_config_path(None)


def _normalize_name_text(text: str) -> str:
    normalized = text.strip()
    normalized = normalized.strip("\"'`。.!?！？：: ")
    return " ".join(normalized.split())


async def _wait_job_done(client: httpx.AsyncClient, job_id: str, max_attempts: int = 60) -> None:
    for _ in range(max_attempts):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        status = response.json()["data"]["status"]

        if status in {"completed", "succeeded"}:
            return
        if status in {"failed", "cancelled", "timed_out"}:
            pytest.fail(f"Job {job_id} 执行失败: {response.json()['data'].get('error_message')}")

        await asyncio.sleep(1)

    pytest.fail(f"Job {job_id} 超时未完成")


def _last_assistant_message(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            return msg.get("content", "")
    pytest.fail("未找到 assistant 消息")


@pytest.mark.asyncio
async def test_agent_name_matches_system_prompt_after_switch(client: httpx.AsyncClient):
    if not os.environ.get("OPENCODE_ZEN_API_KEY"):
        pytest.skip("缺少 OPENCODE_ZEN_API_KEY，跳过真实模型验证")

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Agent Name Alignment Test"},
    )
    assert create_response.status_code == 200
    session_data = create_response.json()["data"]
    session_id = session_data["session_id"]
    assert session_data["current_agent_id"] == "default"

    default_job_resp = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": "请严格只输出你的英文名称，不要输出任何其他内容。"
            },
            "run": {
                "mode": "single_agent"
            },
        },
    )
    assert default_job_resp.status_code == 200
    default_job_id = default_job_resp.json()["data"]["job_id"]
    await _wait_job_done(client, default_job_id)

    messages_resp = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_resp.status_code == 200
    messages = messages_resp.json()["data"]["items"]
    default_reply = _normalize_name_text(_last_assistant_message(messages))
    assert default_reply == "Workspace Assistant", f"默认 agent 名称不匹配: {default_reply}"

    switch_response = await client.patch(
        f"/api/v1/sessions/{session_id}",
        json={"agent_id": "coder"},
    )
    assert switch_response.status_code == 200
    assert switch_response.json()["data"]["current_agent_id"] == "coder"

    coder_job_resp = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": "请严格只输出你的英文名称，不要输出任何其他内容。"
            },
            "run": {
                "mode": "single_agent"
            },
        },
    )
    assert coder_job_resp.status_code == 200
    coder_job_id = coder_job_resp.json()["data"]["job_id"]
    await _wait_job_done(client, coder_job_id)

    messages_resp = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_resp.status_code == 200
    messages = messages_resp.json()["data"]["items"]
    coder_reply = _normalize_name_text(_last_assistant_message(messages))
    assert coder_reply == "Coding Assistant", f"切换后 agent 名称不匹配: {coder_reply}"
