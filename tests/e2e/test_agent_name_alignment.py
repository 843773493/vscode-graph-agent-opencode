from __future__ import annotations

import os

import httpx
import pytest

from tests.e2e.utils import last_assistant_message, normalize_text, requires_real_model, wait_for_job_done


@pytest.mark.asyncio
async def test_agent_name_matches_system_prompt_after_switch(client: httpx.AsyncClient):
    requires_real_model()

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
    await wait_for_job_done(client, default_job_id)

    messages_resp = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_resp.status_code == 200
    messages = messages_resp.json()["data"]["items"]
    default_reply = normalize_text(last_assistant_message(messages))
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
    await wait_for_job_done(client, coder_job_id)

    messages_resp = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_resp.status_code == 200
    messages = messages_resp.json()["data"]["items"]
    coder_reply = normalize_text(last_assistant_message(messages))
    assert coder_reply == "Coding Assistant", f"切换后 agent 名称不匹配: {coder_reply}"
