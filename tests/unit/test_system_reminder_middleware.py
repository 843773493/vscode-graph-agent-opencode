"""System reminder 注入逻辑的单元测试。"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.system_reminder_middleware import SystemReminderMiddleware
from app.schemas.system_reminder import (
    ReminderTriggerContext,
    SystemReminder,
    SystemReminderPosition,
    SystemReminderTrigger,
)
from app.services.infrastructure.system_reminder_triggers import (
    SystemReminderTriggerRegistry,
)


class _FixedTrigger(SystemReminderTrigger):
    def __init__(self, reminders: list[SystemReminder]) -> None:
        self._reminders = reminders

    def produce(self, ctx: ReminderTriggerContext) -> list[SystemReminder]:
        return list(self._reminders)


@pytest.fixture
def registry() -> SystemReminderTriggerRegistry:
    return SystemReminderTriggerRegistry()


@pytest.fixture
def middleware(registry: SystemReminderTriggerRegistry) -> SystemReminderMiddleware:
    return SystemReminderMiddleware(trigger_registry=registry)


def _wrap(content: str) -> str:
    return f"\u003csystem_reminder\u003e\n{content}\n\u003c/system_reminder\u003e"


def test_inject_after_last_assistant(middleware: SystemReminderMiddleware, registry: SystemReminderTriggerRegistry):
    registry.register(_FixedTrigger([
        SystemReminder(content="resume", position=SystemReminderPosition.AFTER_LAST_ASSISTANT),
    ]))

    messages = [
        HumanMessage(content="hello"),
        AIMessage(content="I will call tool"),
    ]
    result = middleware._inject_reminders(messages, [SystemReminder(content="resume", position=SystemReminderPosition.AFTER_LAST_ASSISTANT)])

    assert len(result) == 3
    assert isinstance(result[0], HumanMessage)
    assert isinstance(result[1], AIMessage)
    assert isinstance(result[2], HumanMessage)
    assert result[2].content == _wrap("resume")


def test_inject_after_tool_calls(middleware: SystemReminderMiddleware, registry: SystemReminderTriggerRegistry):
    registry.register(_FixedTrigger([
        SystemReminder(content="tool done", position=SystemReminderPosition.AFTER_TOOL_CALLS),
    ]))

    messages = [
        HumanMessage(content="hello"),
        AIMessage(content="calling tool"),
        ToolMessage(content="2333", tool_call_id="call_1"),
    ]
    result = middleware._inject_reminders(messages, [SystemReminder(content="tool done", position=SystemReminderPosition.AFTER_TOOL_CALLS)])

    assert len(result) == 4
    assert isinstance(result[3], HumanMessage)
    assert result[3].content == _wrap("tool done")


def test_inject_after_last_user(middleware: SystemReminderMiddleware, registry: SystemReminderTriggerRegistry):
    registry.register(_FixedTrigger([
        SystemReminder(content="user context", position=SystemReminderPosition.AFTER_LAST_USER),
    ]))

    messages = [
        HumanMessage(content="hello"),
        AIMessage(content="hi"),
        HumanMessage(content="do it"),
    ]
    result = middleware._inject_reminders(messages, [SystemReminder(content="user context", position=SystemReminderPosition.AFTER_LAST_USER)])

    assert len(result) == 4
    assert isinstance(result[3], HumanMessage)
    assert result[3].content == _wrap("user context")


def test_multiple_positions_sorted(middleware: SystemReminderMiddleware, registry: SystemReminderTriggerRegistry):
    registry.register(_FixedTrigger([
        SystemReminder(content="z", position=SystemReminderPosition.AFTER_LAST_ASSISTANT, priority=2),
        SystemReminder(content="a", position=SystemReminderPosition.AFTER_LAST_ASSISTANT, priority=1),
    ]))

    messages = [
        HumanMessage(content="hello"),
        AIMessage(content="assistant"),
    ]
    reminders = [
        SystemReminder(content="z", position=SystemReminderPosition.AFTER_LAST_ASSISTANT, priority=2),
        SystemReminder(content="a", position=SystemReminderPosition.AFTER_LAST_ASSISTANT, priority=1),
    ]
    result = middleware._inject_reminders(messages, reminders)

    assert len(result) == 4
    assert result[2].content == _wrap("a")
    assert result[3].content == _wrap("z")


def test_no_reminders_returns_original(middleware: SystemReminderMiddleware):
    messages = [HumanMessage(content="hello")]
    result = middleware._inject_reminders(messages, [])
    assert result == messages


def test_trigger_context_passed_to_registry(registry: SystemReminderTriggerRegistry):
    calls: list[ReminderTriggerContext] = []

    class _CaptureTrigger(SystemReminderTrigger):
        def produce(self, ctx: ReminderTriggerContext) -> list[SystemReminder]:
            calls.append(ctx)
            return []

    registry.register(_CaptureTrigger())
    middleware = SystemReminderMiddleware(trigger_registry=registry)

    # We cannot easily call awrap_model_call without a real ModelRequest,
    # but we can exercise _build_trigger_context indirectly by constructing
    # a minimal runtime-like object.
    from unittest.mock import MagicMock
    request = MagicMock()
    request.messages = [HumanMessage(content="hi")]
    request.runtime.configurable = {"session_id": "ses_1", "job_id": "job_1"}

    ctx = middleware._build_trigger_context(request)
    assert ctx.session_id == "ses_1"
    assert ctx.job_id == "job_1"
