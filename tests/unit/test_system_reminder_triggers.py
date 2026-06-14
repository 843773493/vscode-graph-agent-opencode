"""System reminder 触发器单元测试。"""
from __future__ import annotations

import pytest

from app.schemas.system_reminder import ReminderTriggerContext, SystemReminderPosition
from app.services.infrastructure.system_reminder_triggers import (
    InterruptReminderTrigger,
    SystemReminderTriggerRegistry,
    TaskCompletionReminderTrigger,
)


@pytest.fixture
def base_ctx() -> ReminderTriggerContext:
    return ReminderTriggerContext(
        session_id="ses_1",
        job_id="job_1",
        agent_id="default",
        messages=[],
        last_turn_status="ok",
        recent_tool_results=[],
    )


def test_interrupt_trigger_returns_empty_when_ok(base_ctx: ReminderTriggerContext):
    trigger = InterruptReminderTrigger()
    reminders = trigger.produce(base_ctx)
    assert reminders == []


def test_interrupt_trigger_tool_interrupt(base_ctx: ReminderTriggerContext):
    base_ctx.last_turn_status = "interrupted_tool"
    trigger = InterruptReminderTrigger()
    reminders = trigger.produce(base_ctx)

    assert len(reminders) == 1
    assert reminders[0].position == SystemReminderPosition.AFTER_LAST_ASSISTANT
    assert "工具" in reminders[0].content
    assert reminders[0].dedup_key == "interrupt:job_1"


def test_interrupt_trigger_text_interrupt(base_ctx: ReminderTriggerContext):
    base_ctx.last_turn_status = "interrupted_text"
    trigger = InterruptReminderTrigger()
    reminders = trigger.produce(base_ctx)

    assert len(reminders) == 1
    assert reminders[0].position == SystemReminderPosition.AFTER_LAST_ASSISTANT
    assert "文本" in reminders[0].content


def test_task_completion_trigger_empty_when_no_results(base_ctx: ReminderTriggerContext):
    trigger = TaskCompletionReminderTrigger()
    reminders = trigger.produce(base_ctx)
    assert reminders == []


def test_task_completion_trigger_includes_results(base_ctx: ReminderTriggerContext):
    base_ctx.recent_tool_results = [
        {"tool_name": "test_tool", "result": "2333", "interrupted": False},
        {"tool_name": "test_tool_2", "result": "done", "interrupted": True},
    ]
    trigger = TaskCompletionReminderTrigger()
    reminders = trigger.produce(base_ctx)

    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder.position == SystemReminderPosition.AFTER_TOOL_CALLS
    assert "test_tool" in reminder.content
    assert "2333" in reminder.content
    assert "被用户打断" in reminder.content
    assert reminder.dedup_key == "task_completion:job_1"


def test_registry_collects_from_all_triggers():
    registry = SystemReminderTriggerRegistry()
    registry.register(TaskCompletionReminderTrigger())

    ctx = ReminderTriggerContext(
        session_id="ses_1",
        job_id="job_1",
        agent_id="default",
        messages=[],
        last_turn_status="ok",
        recent_tool_results=[{"tool_name": "t", "result": "r", "interrupted": False}],
    )

    reminders = registry.collect(ctx)
    assert len(reminders) == 1
    assert reminders[0].content.startswith("以下工具调用已完成")
