from __future__ import annotations

import json

import httpx
import pytest

from tests.e2e.utils import wait_for_job_done


async def _send_message(client: httpx.AsyncClient, session_id: str, content: str) -> str:
    response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": content},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert response.status_code == 200
    return response.json()["data"]["job_id"]


async def _load_assistant_state_records(
    client: httpx.AsyncClient,
    session_id: str,
) -> list[dict]:
    state_response = await client.get(f"/api/v1/sessions/{session_id}/agent-state/messages")
    assert state_response.status_code == 200
    snapshot = state_response.json()["data"]
    records = [
        json.loads(line)
        for line in snapshot["jsonl"].splitlines()
        if line.strip()
    ]
    return [
        record for record in records if record.get("role") == "assistant"
    ]


@pytest.mark.asyncio
async def test_agent_state_keeps_reasoning_and_final_text_separate(
    client: httpx.AsyncClient,
):
    """真实后端闭环：Agent State 中不应把模型思考和最终回复混成同一个字符串。"""
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Agent State Reasoning Split E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    first_job_id = await _send_message(client, session_id, "请只回复 OK，不要解释。")
    first_job = await wait_for_job_done(client, first_job_id)
    assert first_job["status"] in {"completed", "succeeded"}

    assistant_records = await _load_assistant_state_records(client, session_id)
    assert assistant_records, "Agent State 中应包含 assistant 消息"

    first_assistant_content = assistant_records[-1]["content"]
    assert isinstance(first_assistant_content, list)
    assert first_assistant_content[0]["type"] == "reasoning"
    assert isinstance(first_assistant_content[0]["reasoning"], str)
    assert first_assistant_content[0]["id"].startswith("part_")
    assert first_assistant_content[0]["index"] == 0
    assert first_assistant_content[1]["type"] == "text"
    assert first_assistant_content[1]["text"] == "OK"
    assert first_assistant_content[1]["id"].startswith("part_")
    assert first_assistant_content[1]["index"] == 1
    assert first_assistant_content[0]["id"] != first_assistant_content[1]["id"]
    assert assistant_records[-1]["response_metadata"]["phase"] == "final_answer"

    traces_response = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert traces_response.status_code == 200
    text_start_part_ids = {
        trace["part_id"]
        for trace in traces_response.json()["data"]
        if trace.get("type") == "text_start"
    }
    assert first_assistant_content[0]["id"] in text_start_part_ids
    assert first_assistant_content[1]["id"] in text_start_part_ids

    second_job_id = await _send_message(
        client,
        session_id,
        "上一轮你的最终回复是什么？只回复最终回复内容。",
    )
    second_job = await wait_for_job_done(client, second_job_id)
    assert second_job["status"] in {"completed", "succeeded"}

    assistant_records = await _load_assistant_state_records(client, session_id)
    second_assistant_content = assistant_records[-1]["content"]
    assert isinstance(second_assistant_content, list)
    assert second_assistant_content[0]["type"] == "reasoning"
    assert second_assistant_content[0]["id"].startswith("part_")
    assert second_assistant_content[0]["index"] == 0
    assert second_assistant_content[1]["type"] == "text"
    assert second_assistant_content[1]["text"]
    assert second_assistant_content[1]["id"].startswith("part_")
    assert second_assistant_content[1]["index"] == 1
    assert assistant_records[-1]["response_metadata"]["phase"] == "final_answer"
