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
