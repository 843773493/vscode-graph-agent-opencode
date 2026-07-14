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
from tests.e2e.utils import wait_for_job_done


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


def _append_checkpoint_messages(
    *,
    workspace_root: str,
    session_id: str,
    pair_count: int,
) -> None:
    """保留中间件私有状态，仅向现有会话追加用于压缩的历史消息。"""
    saver = FileSystemCheckpointSaver(
        base_dir=Path(workspace_root) / ".boxteam" / "checkpoints"
    )
    checkpoint_tuple = saver.get_tuple(build_checkpoint_config(session_id))
    assert checkpoint_tuple is not None

    checkpoint = checkpoint_tuple.checkpoint.copy()
    channel_values = dict(checkpoint.get("channel_values", {}))
    messages = list(channel_values.get("messages", []))
    for index in range(pair_count):
        messages.append(HumanMessage(content=f"AGENTS 压缩测试用户消息 {index}"))
        messages.append(AIMessage(content=f"AGENTS 压缩测试助手消息 {index}"))
    channel_values["messages"] = messages
    checkpoint["channel_values"] = channel_values
    checkpoint["id"] = str(uuid.uuid4())

    channel_versions = dict(checkpoint.get("channel_versions", {}))
    messages_version = saver.get_next_version(
        channel_versions.get("messages"),
        None,
    )
    channel_versions["messages"] = messages_version
    checkpoint["channel_versions"] = channel_versions
    checkpoint["updated_channels"] = ["messages"]
    saver.put(
        checkpoint_tuple.config,
        checkpoint,
        metadata={"source": "e2e_append", "step": -1, "writes": {}},
        new_versions={"messages": messages_version},
    )


async def _send_message(
    client: httpx.AsyncClient,
    session_id: str,
    content: str,
) -> str:
    response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": content},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert response.status_code == 200
    job_id = response.json()["data"]["job_id"]
    job = await wait_for_job_done(client, job_id, max_attempts=90)
    assert job["status"] in {"completed", "succeeded"}
    return job_id


async def _job_request_log(
    client: httpx.AsyncClient,
    session_id: str,
    job_id: str,
) -> dict:
    response = await client.get(
        f"/api/v1/sessions/{session_id}/llm-request-logs"
    )
    assert response.status_code == 200
    matching_logs = [
        log for log in response.json()["data"] if log.get("job_id") == job_id
    ]
    assert matching_logs
    return matching_logs[-1]["request"]


def _message_content_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TypeError(f"模型日志消息 content 类型无效: {type(content).__name__}")
    texts: list[str] = []
    for block in content:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return "\n".join(texts)


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

    compact_response = await client.post(
        f"/api/v1/sessions/{session_id}/compact",
        timeout=120,
    )
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


@pytest.mark.asyncio
async def test_workspace_agents_change_preserves_system_prompt_until_compaction(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    workspace_root = Path(e2e_workspace_root_path)
    agents_path = workspace_root / "AGENTS.md"
    initial_content = "# E2E AGENTS\n\n始终遵循 agents-cache-version-one。\n"
    changed_content = "# E2E AGENTS\n\n始终遵循 agents-cache-version-two。\n"
    agents_path.write_text(initial_content, encoding="utf-8")

    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "AGENTS Prompt Cache E2E"},
    )
    assert create_response.status_code == 200
    session_id = create_response.json()["data"]["session_id"]

    first_job_id = await _send_message(
        client,
        session_id,
        "请只回复 FIRST_OK，不要调用工具。",
    )
    first_request = await _job_request_log(client, session_id, first_job_id)
    first_system = first_request["system_message"]
    assert first_system is not None
    assert "agents-cache-version-one" in _message_content_text(first_system)

    agents_path.write_text(changed_content, encoding="utf-8")
    second_job_id = await _send_message(
        client,
        session_id,
        "请只回复 SECOND_OK，不要调用工具。",
    )
    second_request = await _job_request_log(client, session_id, second_job_id)
    assert second_request["system_message"] == first_system
    second_messages_text = "\n".join(
        _message_content_text(message) for message in second_request["messages"]
    )
    assert "<system_reminder>" in second_messages_text
    assert "workspace_agents_md_change" in second_messages_text
    assert "+始终遵循 agents-cache-version-two。" in second_messages_text

    _append_checkpoint_messages(
        workspace_root=e2e_workspace_root_path,
        session_id=session_id,
        pair_count=5,
    )
    compact_response = await client.post(
        f"/api/v1/sessions/{session_id}/compact",
        timeout=120,
    )
    assert compact_response.status_code == 200
    assert compact_response.json()["data"]["status"] == "compacted"

    third_job_id = await _send_message(
        client,
        session_id,
        "请只回复 THIRD_OK，不要调用工具。",
    )
    third_request = await _job_request_log(client, session_id, third_job_id)
    third_system = third_request["system_message"]
    assert third_system is not None
    third_system_text = _message_content_text(third_system)
    assert "agents-cache-version-two" in third_system_text
    assert "agents-cache-version-one" not in third_system_text
    assert third_system != first_system
