from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.schemas.event import ModelTokenUsagePayload
from app.services.business.message_service import MessageService
from app.services.business.reasoning_checkpoint_service import (
    persist_standard_assistant_checkpoint,
)


@pytest.mark.asyncio
async def test_persist_standard_assistant_checkpoint_rewrites_latest_message(tmp_path):
    session_id = "sess_standard_assistant"
    reasoning_text = "用户只要求回复 OK。"
    final_text = "OK"
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = build_checkpoint_config(session_id)
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(content="只回复 OK"),
                AIMessage(content=final_text, name="default"),
            ],
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-mixed",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    changed = persist_standard_assistant_checkpoint(
        checkpointer=saver,
        session_id=session_id,
        reasoning_text=reasoning_text,
        final_text=final_text,
        token_usage=ModelTokenUsagePayload(
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            cache_read_input_tokens=80,
            model_calls=2,
            reported_model_calls=2,
        ),
    )

    assert changed is True
    latest = await saver.aget_tuple(config)
    assert latest is not None
    messages = latest.checkpoint["channel_values"]["messages"]
    assistant = messages[-1]
    assert isinstance(assistant, AIMessage)
    assert assistant.response_metadata["phase"] == "final_answer"
    assert assistant.content == [
        {"type": "reasoning", "reasoning": reasoning_text},
        {"type": "text", "text": final_text},
    ]

    messages_page = await MessageService(checkpointer=saver).list(session_id, limit=10)
    assert messages_page.items[-1].content == final_text
    assert messages_page.items[-1].metadata["token_usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cache_read_input_tokens": 80,
        "model_calls": 2,
        "reported_model_calls": 2,
    }

    message_service = MessageService(checkpointer=saver)
    state_snapshot = await message_service.get_agent_state_messages(session_id)
    records = [json.loads(line) for line in state_snapshot.jsonl.splitlines()]
    state_assistant = records[-1]
    assert state_assistant["role"] == "assistant"
    assert state_assistant["response_metadata"]["phase"] == "final_answer"
    assert state_assistant["content"] == [
        {"type": "reasoning", "reasoning": reasoning_text},
        {"type": "text", "text": final_text},
    ]


@pytest.mark.asyncio
async def test_persist_checkpoint_preserves_existing_system_reminder_in_agent_state(tmp_path):
    session_id = "sess_system_reminder_state"
    first_reasoning = "先调用工具。"
    final_reasoning = "只回复工具 stdout。"
    final_text = "OK"
    reminder = "以下工具调用已完成，请在生成回复时参考其结果。"
    tool_call = {
        "name": "python_exec",
        "args": {"code": "print('OK')"},
        "id": "call_1",
        "type": "tool_call",
    }
    user_message = HumanMessage(content="调用工具")
    tool_call_message = AIMessage(
        content=[{"type": "reasoning", "reasoning": first_reasoning}],
        tool_calls=[tool_call],
        name="default",
        response_metadata={"phase": "commentary"},
    )
    tool_message = ToolMessage(
        content='{"stdout":"OK\\n"}',
        tool_call_id="call_1",
        name="python_exec",
    )
    final_message = AIMessage(content=final_text, name="default")

    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = build_checkpoint_config(session_id)
    checkpoint = {
        "channel_values": {
            "messages": [
                user_message,
                tool_call_message,
                tool_message,
                HumanMessage(
                    content=f"<system_reminder>\n{reminder}\n</system_reminder>"
                ),
                final_message,
            ],
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-reminder",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1},
    )

    changed = persist_standard_assistant_checkpoint(
        checkpointer=saver,
        session_id=session_id,
        reasoning_text=final_reasoning,
        final_text=final_text,
    )

    assert changed is True
    state_snapshot = await MessageService(checkpointer=saver).get_agent_state_messages(
        session_id
    )
    records = [json.loads(line) for line in state_snapshot.jsonl.splitlines()]
    assert len(records) == 5
    assert records[3]["role"] == "user"
    assert "<system_reminder>" in records[3]["content"]
    assert reminder in records[3]["content"]
    assert records[-1]["content"] == [
        {"type": "reasoning", "reasoning": final_reasoning},
        {"type": "text", "text": final_text},
    ]
    assert first_reasoning not in records[-1]["content"][0]["reasoning"]

    visible_messages = await MessageService(checkpointer=saver).list(
        session_id,
        limit=10,
    )
    assert all("<system_reminder>" not in item.content for item in visible_messages.items)
