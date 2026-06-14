"""System reminder 触发器注册表与内置触发器实现。"""
from __future__ import annotations

from app.schemas.system_reminder import (
    ReminderTriggerContext,
    SystemReminder,
    SystemReminderPosition,
    SystemReminderTrigger,
    ToolResultSnapshot,
)


class SystemReminderTriggerRegistry:
    """管理所有 system_reminder 触发器。"""

    def __init__(self) -> None:
        self._triggers: list[SystemReminderTrigger] = []

    def register(self, trigger: SystemReminderTrigger) -> None:
        self._triggers.append(trigger)

    def unregister(self, trigger: SystemReminderTrigger) -> None:
        self._triggers.remove(trigger)

    def collect(self, ctx: ReminderTriggerContext) -> list[SystemReminder]:
        reminders: list[SystemReminder] = []
        for trigger in self._triggers:
            reminders.extend(trigger.produce(ctx))
        return reminders


class InterruptReminderTrigger:
    """当上一 turn 被用户打断时，注入恢复/停止提醒。"""

    def produce(self, ctx: ReminderTriggerContext) -> list[SystemReminder]:
        if ctx.last_turn_status not in {"interrupted_tool", "interrupted_text"}:
            return []

        if ctx.last_turn_status == "interrupted_tool":
            content = "用户在你调用工具的过程中打断了。请停止当前工具调用，直接根据已有信息回复用户。"
        else:
            content = "用户在文本生成过程中打断了。请不要继续之前的回复，直接回应用户的最新请求。"

        return [
            SystemReminder(
                content=content,
                position=SystemReminderPosition.AFTER_LAST_ASSISTANT,
                dedup_key=f"interrupt:{ctx.job_id}",
            )
        ]


class TaskCompletionReminderTrigger:
    """工具调用链完成后，注入结果摘要提醒。"""

    def produce(self, ctx: ReminderTriggerContext) -> list[SystemReminder]:
        if not ctx.recent_tool_results:
            return []

        lines: list[str] = ["以下工具调用已完成，请在生成回复时参考其结果："]
        for raw in ctx.recent_tool_results:
            snapshot = raw if isinstance(raw, ToolResultSnapshot) else ToolResultSnapshot(**raw)
            status = "（被用户打断）" if snapshot.interrupted else ""
            lines.append(f"- {snapshot.tool_name}{status}: {snapshot.result}")

        return [
            SystemReminder(
                content="\n".join(lines),
                position=SystemReminderPosition.AFTER_TOOL_CALLS,
                dedup_key=f"task_completion:{ctx.job_id}",
            )
        ]


class BackgroundMessageReminderTrigger:
    """将后台 interrupt 消息转换为 system_reminder。

    TODO: 当前为占位实现，待 BackgroundMessageBus 与触发上下文集成后完善。
    """

    def produce(self, ctx: ReminderTriggerContext) -> list[SystemReminder]:
        return []


def build_default_trigger_registry() -> SystemReminderTriggerRegistry:
    """构建默认触发器注册表。"""

    registry = SystemReminderTriggerRegistry()
    registry.register(InterruptReminderTrigger())
    registry.register(TaskCompletionReminderTrigger())
    registry.register(BackgroundMessageReminderTrigger())
    return registry
