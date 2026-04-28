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
def setup_tool_denylist_config():
    from app.services.config_service import ConfigService, set_config_path

    ConfigService.reset_instance()
    config_path = Path(__file__).resolve().parents[2] / "configs" / "tests" / "tool_denylist_check.jsonc"
    set_config_path(str(config_path))
    yield
    ConfigService.reset_instance()
    set_config_path(None)


def _normalize_answer(text: str) -> str:
    normalized = text.strip()
    normalized = normalized.strip('"\'`。.!?！？：: ')
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
            content = msg.get("content", "")
            if content:
                return content
    pytest.fail("未找到非空 assistant 消息")


@pytest.mark.asyncio
async def test_denied_tools_are_hidden_from_model(client: httpx.AsyncClient):
    if not os.environ.get("OPENCODE_ZEN_API_KEY"):
        pytest.skip("缺少 OPENCODE_ZEN_API_KEY，跳过真实模型验证")

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Tool Denylist Visibility Test"},
    )
    assert create_response.status_code == 200
    session_data = create_response.json()["data"]
    session_id = session_data["session_id"]
    assert session_data["current_agent_id"] == "coder"

    prompt = (
        "请判断你是否拥有 send_message_to_session 和 edit_file 这两个工具。"
        "如果你有，请列出工具名称；如果没有，请且只能回答：否。不要输出其他内容。"
    )

    job_resp = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": prompt},
            "run": {"mode": "single_agent"},
        },
    )
    assert job_resp.status_code == 200
    job_id = job_resp.json()["data"]["job_id"]
    await _wait_job_done(client, job_id)

    messages_resp = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_resp.status_code == 200
    messages = messages_resp.json()["data"]["items"]
    reply = _normalize_answer(_last_assistant_message(messages))

    assert reply == "否", f"工具 denylist 未生效，模型回复: {reply}"
