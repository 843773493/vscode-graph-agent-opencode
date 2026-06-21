"""MessageService 与 checkpoint 集成单元测试。"""
from __future__ import annotations

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
    # 顺序：先 reasoning 摘要（<think>...</think>），再 text
    assert assistant.content == "<think>\n先思考一下\n</think>最终回答"
    assert assistant.metadata.get("reasoning_id") == "rs_abc123"
    reasoning_blocks = assistant.metadata.get("reasoning_blocks")
    assert isinstance(reasoning_blocks, list) and len(reasoning_blocks) == 1
    assert reasoning_blocks[0]["type"] == "reasoning"
    assert reasoning_blocks[0]["summary"][0]["text"] == "先思考一下"


@pytest.mark.asyncio
async def test_message_service_extracts_opencode_zen_string_reasoning(tmp_path):
    """验证 opencode_zen 路径下的字符串 content + kind=reasoning 被识别。"""
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_zen", "checkpoint_ns": ""}}
    reasoning_msg = AIMessage(
        content="我在思考",
        additional_kwargs={"kind": "reasoning"},
    )
    checkpoint = {
        "channel_values": {"messages": [HumanMessage(content="hi"), reasoning_msg]},
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-zen",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    service = MessageService(checkpointer=saver)
    messages = await service.list(session_id="sess_zen", limit=10)

    assistant = messages.items[1]
    assert assistant.content == "<think>\n我在思考\n</think>"
    reasoning_blocks = assistant.metadata.get("reasoning_blocks")
    assert isinstance(reasoning_blocks, list) and len(reasoning_blocks) == 1
    assert reasoning_blocks[0]["type"] == "reasoning"


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
