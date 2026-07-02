"""MessageService 与 checkpoint 集成单元测试。"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.services.business.message_service import MessageService


@pytest.mark.asyncio
async def test_message_service_loads_history_from_checkpoint(tmp_path):
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess1", "checkpoint_ns": ""}}
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(content="hi"),
                AIMessage(content="hello", response_metadata={"phase": "text"}),
            ],
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-1",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess1", limit=10)

    assert len(messages.items) == 2
    assert messages.items[0].role.value == "user"
    assert messages.items[0].content == "hi"
    assert messages.items[1].role.value == "assistant"
    assert messages.items[1].content == "hello"
    assert messages.items[1].metadata.get("phase") == "text"


@pytest.mark.asyncio
async def test_message_service_returns_empty_when_no_checkpoint(tmp_path):
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_noexist", limit=10)
    assert messages.items == []


@pytest.mark.asyncio
async def test_message_service_extracts_responses_api_reasoning_blocks(tmp_path):
    """验证 Responses API 路径下产生的 reasoning 块被正确提取到 metadata。"""
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_reasoning", "checkpoint_ns": ""}}
    reasoning_msg = AIMessage(
        content=[
            {
                "type": "reasoning",
                "id": "rs_abc123",
                "summary": [{"type": "summary_text", "text": "先思考一下"}],
            },
            {"type": "output_text", "text": "最终回答"},
        ],
        response_metadata={"message_id": "msg_001"},
    )
    checkpoint = {
        "channel_values": {"messages": [HumanMessage(content="hi"), reasoning_msg]},
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-r",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_reasoning", limit=10)

    assert len(messages.items) == 2
    assistant = messages.items[1]
    assert assistant.content == "最终回答"
    assert assistant.metadata.get("reasoning_id") == "rs_abc123"
    content_blocks = assistant.metadata.get("content_blocks")
    assert isinstance(content_blocks, list)
    assert content_blocks == [
        {"type": "reasoning", "reasoning": "先思考一下", "extras": {"id": "rs_abc123"}},
        {"type": "text", "text": "最终回答"},
    ]


@pytest.mark.asyncio
async def test_agent_state_renders_standard_reasoning_tool_call_message(tmp_path):
    """工具调用前的 reasoning 应来自标准 content block。"""
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_tool_reasoning", "checkpoint_ns": ""}}
    reasoning_text = "用户想查看当前系统时间。"
    tool_call = {
        "name": "python_exec",
        "args": {"code": "print('ok')"},
        "id": "call_001",
        "type": "tool_call",
    }
    reasoning_msg = AIMessage(
        content=[{"type": "reasoning", "reasoning": reasoning_text}],
        tool_calls=[tool_call],
        response_metadata={"phase": "commentary"},
        name="default",
    )
    checkpoint = {
        "channel_values": {"messages": [HumanMessage(content="现在几点"), reasoning_msg]},
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-tool-reasoning",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_tool_reasoning", limit=10)
    assistant = messages.items[1]
    assert assistant.content == ""
    assert assistant.metadata["content_blocks"] == [
        {"type": "reasoning", "reasoning": reasoning_text}
    ]

    state_snapshot = await service.get_agent_state_messages("sess_tool_reasoning")
    records = [json.loads(line) for line in state_snapshot.jsonl.splitlines()]
    state_assistant = records[1]
    assert state_assistant["content"] == [
        {"type": "reasoning", "reasoning": reasoning_text}
    ]
    assert state_assistant["tool_calls"] == [tool_call]
    assert state_assistant["response_metadata"]["phase"] == "commentary"
    assert "additional_kwargs" not in state_assistant


@pytest.mark.asyncio
async def test_message_service_refusal_block(tmp_path):
    """验证 refusal 块被识别并标记。"""
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_refusal", "checkpoint_ns": ""}}
    msg = AIMessage(
        content=[{"type": "refusal", "refusal": "我拒绝回答"}],
    )
    checkpoint = {
        "channel_values": {"messages": [HumanMessage(content="hi"), msg]},
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-rf",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_refusal", limit=10)

    assert messages.items[1].content == "[拒绝]我拒绝回答"
