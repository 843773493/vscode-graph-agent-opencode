"""SystemReminderMiddleware 单元测试。"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.system_reminder_middleware import SystemReminderMiddleware
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.services.infrastructure.system_reminder_triggers import (
    build_default_trigger_registry,
)


class _FakeRuntime:
    configurable = {"session_id": "sess_interrupt", "thread_id": "sess_interrupt"}


class _FakeRequest:
    runtime = _FakeRuntime()
    messages: list = []


@pytest.mark.asyncio
async def test_middleware_appends_interrupt_reminder_to_last_assistant(tmp_path):
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_interrupt", "checkpoint_ns": ""}}
    checkpoint = {
        "channel_values": {
            "messages": [HumanMessage(content="hi"), AIMessage(content="hello")],
            "__boxteam_interrupt__": {
                "phase": "text",
                "tool_name": None,
                "interrupted_at": "2024-01-01T00:00:00",
            },
        },
        "channel_versions": {"messages": 1, "__boxteam_interrupt__": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-1",
    }
    await saver.aput(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}},
        {"messages": 1, "__boxteam_interrupt__": 1},
    )

    registry = build_default_trigger_registry()
    middleware = SystemReminderMiddleware(
        trigger_registry=registry, checkpointer=saver
    )

    request = _FakeRequest()
    request.messages = [HumanMessage(content="hi"), AIMessage(content="hello")]

    captured: list = []

    def handler(req):
        captured.extend(req.messages)
        return None

    middleware.wrap_model_call(request, handler)

    assert len(captured) == 2
    ai_msg = captured[1]
    assert isinstance(ai_msg, AIMessage)
    assert "<system_reminder>" in ai_msg.content
    assert "文本生成" in ai_msg.content
    assert ai_msg.response_metadata.get("phase") == "text"

    # 标记应被清除
    tup = saver.get_tuple(config)
    assert "__boxteam_interrupt__" not in tup.checkpoint.get("channel_values", {})


@pytest.mark.asyncio
async def test_middleware_noop_when_no_interrupt_marker(tmp_path):
    saver = FileSystemCheckpointSaver(base_dir=tmp_path)
    config = {"configurable": {"thread_id": "sess_no_interrupt", "checkpoint_ns": ""}}
    checkpoint = {
        "channel_values": {
            "messages": [HumanMessage(content="hi"), AIMessage(content="hello")],
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

    registry = build_default_trigger_registry()
    middleware = SystemReminderMiddleware(
        trigger_registry=registry, checkpointer=saver
    )

    request = _FakeRequest()
    request.messages = [HumanMessage(content="hi"), AIMessage(content="hello")]

    captured: list = []

    def handler(req):
        captured.extend(req.messages)
        return None

    middleware.wrap_model_call(request, handler)

    assert len(captured) == 2
    assert captured[0].content == "hi"
    assert captured[1].content == "hello"
    assert "<system_reminder>" not in captured[1].content
