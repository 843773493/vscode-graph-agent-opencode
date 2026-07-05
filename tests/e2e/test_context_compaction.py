#!/usr/bin/env python3
"""会话上下文压缩端到端测试。"""
from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver


def _seed_checkpoint_messages(
    *,
    workspace_root: str,
    session_id: str,
    pair_count: int = 5,
) -> None:
    saver = FileSystemCheckpointSaver(
        base_dir=Path(workspace_root) / ".boxteam" / "checkpoints"
    )
    messages = []
    for index in range(pair_count):
        messages.append(
            HumanMessage(
                content=f"第 {index} 轮用户消息：请记住 compact-e2e-{index}",
            )
        )
        messages.append(
            AIMessage(
                content=f"第 {index} 轮助手回复：已记录 compact-e2e-{index}",
            )
        )

    messages_version = saver.get_next_version(None, None)
    checkpoint = {
        "id": str(uuid.uuid4()),
        "channel_values": {"messages": messages},
        "channel_versions": {"messages": messages_version},
        "updated_channels": ["messages"],
    }
    saver.put(
        build_checkpoint_config(session_id),
        checkpoint,
        metadata={"source": "e2e_seed", "step": -1, "writes": {}},
        new_versions={"messages": messages_version},
    )


@pytest.mark.asyncio
async def test_session_context_compact_writes_summarization_event(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Context Compact E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    tools_response = await client.get("/api/v1/tools")
    assert tools_response.status_code == 200
    tools = tools_response.json()["data"]
    compact_tool = next(
        tool for tool in tools if tool["tool_id"] == "compact_conversation"
    )
    assert compact_tool["parameters"]["properties"] == {}

    _seed_checkpoint_messages(
        workspace_root=e2e_workspace_root_path,
        session_id=session_id,
        pair_count=5,
    )

    compact_response = await client.post(f"/api/v1/sessions/{session_id}/compact")
    assert compact_response.status_code == 200
    result = compact_response.json()["data"]

    assert result["status"] == "compacted"
    assert result["before_message_count"] == 10
    assert result["effective_message_count_before"] == 10
    assert result["effective_message_count_after"] < result["effective_message_count_before"]
    assert result["summarized_message_count"] > 0
    assert result["retained_message_count"] > 0
    assert result["summary"]
    assert result["history_file_path"] == f"/.boxteam/conversation_history/{session_id}.md"

    history_file = (
        Path(e2e_workspace_root_path)
        / ".boxteam"
        / "conversation_history"
        / f"{session_id}.md"
    )
    assert history_file.exists()
    history_content = history_file.read_text(encoding="utf-8")
    assert "Summarized at" in history_content
    assert "compact-e2e-0" in history_content

    saver = FileSystemCheckpointSaver(
        base_dir=Path(e2e_workspace_root_path) / ".boxteam" / "checkpoints"
    )
    checkpoint = saver.get_tuple(build_checkpoint_config(session_id))
    assert checkpoint is not None
    channel_values = checkpoint.checkpoint["channel_values"]
    compact_event = channel_values.get("_summarization_event")
    assert compact_event is not None
    assert compact_event["cutoff_index"] == result["summarized_message_count"]
    assert compact_event["file_path"] == result["history_file_path"]
    assert compact_event["summary_message"].additional_kwargs.get("lc_source") == "summarization"
