from __future__ import annotations

import httpx
import pytest

from tests.e2e.utils import last_assistant_message, normalize_text, wait_for_job_done


@pytest.mark.asyncio
async def test_denied_tools_are_hidden_from_model(client: httpx.AsyncClient):

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Tool Denylist Visibility Test", "agent_id": "coder"},
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
    await wait_for_job_done(client, job_id)

    messages_resp = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_resp.status_code == 200
    messages = messages_resp.json()["data"]["items"]
    reply = normalize_text(last_assistant_message(messages))

    assert reply == "否", f"工具 denylist 未生效，模型回复: {reply}"
