"""SystemReminderMiddleware 单元测试。

说明：
    历史上的 `__boxteam_interrupt__` 自造 channel + 手工 `checkpointer.put`
    已被删除（见 `app/agents/system_reminder_middleware.py` 顶部 docstring）。
    现在 reminder 注入完全由 `SystemReminderTriggerRegistry` 决定 —— 主要是
    `InterruptReminderTrigger`，它依赖 `last_turn_status` contextvar。
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.system_reminder_middleware import SystemReminderMiddleware
from app.core import job_context
from app.schemas.system_reminder import (
    ReminderTriggerContext,
    SystemReminder,
    SystemReminderPosition,
    SystemReminderTrigger,
)
from app.services.infrastructure.system_reminder_triggers import (
    InterruptReminderTrigger,
    build_default_trigger_registry,
)


class _FakeRuntime:
    configurable = {"session_id": "sess_1", "thread_id": "sess_1"}


class _FakeRequest:
    def __init__(self, messages):
        self.runtime = _FakeRuntime()
        self.messages = list(messages)


def _make_middleware(registry) -> SystemReminderMiddleware:
    return SystemReminderMiddleware(trigger_registry=registry)


def test_middleware_appends_interrupt_reminder_when_last_turn_was_interrupted():
    """用户中断后重新提问：trigger_registry 注入 reminder；middleware 把它拼到最后一条 assistant 末尾。"""
    registry = build_default_trigger_registry()
    registry.register(InterruptReminderTrigger())
    middleware = _make_middleware(registry)

    token = job_context.set_last_turn_status("interrupted_text")
    try:
        request = _FakeRequest([
            HumanMessage(content="hi"),
            AIMessage(content="hello"),
        ])

        captured: list = []

        def handler(req):
            captured.extend(req.messages)
            return None

        middleware.wrap_model_call(request, handler)
    finally:
        job_context.reset_last_turn_status(token)

    assert len(captured) == 2
    ai_msg = captured[1]
    assert isinstance(ai_msg, AIMessage)
    assert "<system_reminder>" in ai_msg.content
    assert "文本" in ai_msg.content


def test_middleware_noop_when_no_reminders():
    """无 reminder 时 messages 不被修改。"""
    registry = build_default_trigger_registry()
    middleware = _make_middleware(registry)

    request = _FakeRequest([
        HumanMessage(content="hi"),
        AIMessage(content="hello"),
    ])

    captured: list = []

    def handler(req):
        captured.extend(req.messages)
        return None

    middleware.wrap_model_call(request, handler)

    assert len(captured) == 2
    assert captured[0].content == "hi"
    assert captured[1].content == "hello"
    assert "<system_reminder>" not in captured[1].content


def test_middleware_passes_messages_through_unchanged_when_no_last_assistant():
    """没有最后一条 assistant 时，AFTER_LAST_ASSISTANT reminder 应回退为追加 HumanMessage。"""
    class _CustomTrigger(SystemReminderTrigger):
        def produce(self, ctx: ReminderTriggerContext) -> list[SystemReminder]:
            return [
                SystemReminder(
                    content="上下文提醒",
                    position=SystemReminderPosition.AFTER_LAST_ASSISTANT,
                    dedup_key="custom:test",
                )
            ]

    registry = build_default_trigger_registry()
    registry.register(_CustomTrigger())
    middleware = _make_middleware(registry)

    request = _FakeRequest([HumanMessage(content="hi")])

    captured: list = []

    def handler(req):
        captured.extend(req.messages)
        return None

    middleware.wrap_model_call(request, handler)

    # 没有 assistant → reminder 追加为新的 HumanMessage
    assert len(captured) == 2
    assert isinstance(captured[1], HumanMessage)
    assert "<system_reminder>" in captured[1].content


def test_middleware_no_checkpointer_dependency():
    """构造 middleware 不再需要 checkpointer 参数。"""
    registry = build_default_trigger_registry()
    middleware = SystemReminderMiddleware(trigger_registry=registry)
    assert middleware is not None
