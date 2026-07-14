from __future__ import annotations

from pathlib import Path
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver


@pytest.mark.asyncio
async def test_fork_context_creates_child_without_copying_session_side_data(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "上下文 Fork E2E"},
    )
    assert create_response.status_code == 200
    source = create_response.json()["data"]
    source_session_id = source["session_id"]

    checkpointer = FileSystemCheckpointSaver(
        base_dir=Path(e2e_workspace_root_path) / ".boxteam" / "checkpoints"
    )
    checkpoint = {
        "v": 1,
        "id": "e2e-source-checkpoint",
        "ts": "2026-07-13T00:00:00+00:00",
        "channel_values": {
            "messages": [
                HumanMessage(content="项目代号是 ORBIT"),
                AIMessage(content="我会记住项目代号 ORBIT"),
            ],
            "scratchpad": {"project_code": "ORBIT"},
        },
        "channel_versions": {"messages": 1, "scratchpad": 1},
        "versions_seen": {},
        "pending_sends": [],
        "updated_channels": ["messages", "scratchpad"],
    }
    await checkpointer.aput(
        build_checkpoint_config(source_session_id),
        checkpoint,
        {"source": "loop", "step": 1, "parents": {}},
        {"messages": 1, "scratchpad": 1},
    )

    fork_response = await client.post(
        f"/api/v1/sessions/{source_session_id}/fork-context"
    )
    assert fork_response.status_code == 200, fork_response.text
    child = fork_response.json()["data"]
    child_session_id = child["session_id"]

    assert child["parent_session_id"] == source_session_id
    assert child["current_agent_id"] == source["current_agent_id"]
    assert child["title"] == "上下文 Fork E2E（上下文副本）"

    source_state_response = await client.get(
        f"/api/v1/sessions/{source_session_id}/agent-state/messages"
    )
    child_state_response = await client.get(
        f"/api/v1/sessions/{child_session_id}/agent-state/messages"
    )
    assert source_state_response.status_code == 200
    assert child_state_response.status_code == 200
    source_state = source_state_response.json()["data"]
    child_state = child_state_response.json()["data"]
    assert child_state["message_count"] == source_state["message_count"] == 2
    source_records = [
        json.loads(line) for line in source_state["jsonl"].splitlines() if line
    ]
    child_records = [
        json.loads(line) for line in child_state["jsonl"].splitlines() if line
    ]
    assert [record["content"] for record in child_records] == [
        record["content"] for record in source_records
    ]
    assert all(
        record["response_metadata"]["context_fork_source_session_id"]
        == source_session_id
        for record in child_records
    )

    child_messages_response = await client.get(
        f"/api/v1/sessions/{child_session_id}/messages"
    )
    assert child_messages_response.status_code == 200
    child_messages = child_messages_response.json()["data"]["items"]
    assert [message["content"] for message in child_messages] == [
        "项目代号是 ORBIT",
        "我会记住项目代号 ORBIT",
    ]

    child_checkpoint = await checkpointer.aget_tuple(
        build_checkpoint_config(child_session_id)
    )
    source_checkpoint = await checkpointer.aget_tuple(
        build_checkpoint_config(source_session_id)
    )
    assert child_checkpoint is not None
    assert source_checkpoint is not None
    assert child_checkpoint.checkpoint["id"] != source_checkpoint.checkpoint["id"]
    assert child_checkpoint.checkpoint["channel_values"]["scratchpad"] == {
        "project_code": "ORBIT"
    }

    traces_response = await client.get(
        f"/api/v1/sessions/{child_session_id}/traces"
    )
    resources_response = await client.get(
        f"/api/v1/sessions/{child_session_id}/resources"
    )
    assert traces_response.status_code == 200
    assert traces_response.json()["data"] == []
    assert resources_response.status_code == 200
    assert resources_response.json()["data"]["items"] == []
