from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Final


_UNSET: Final = object()


@dataclass
class InterruptibleState:
    """某个 session 当前可打断的执行状态。"""

    phase: str | None = None
    tool_name: str | None = None
    current_text: str = ""
    user_interrupt_reminder_injected: bool = False
    cancellation_reason: str | None = None
    active_tools_by_run_id: dict[str, str] = field(default_factory=dict)

    @property
    def active_tool_names(self) -> tuple[str, ...]:
        """按启动顺序返回仍在执行的工具名。"""
        return tuple(self.active_tools_by_run_id.values())


class SessionInterruptState:
    """按 session_id 维护当前可打断阶段，供跨 task 查询。"""

    _states: Dict[str, InterruptibleState] = {}

    @classmethod
    def get(cls, session_id: str) -> InterruptibleState:
        return cls._states.get(session_id, InterruptibleState())

    @classmethod
    def set(
        cls,
        session_id: str,
        *,
        phase: str | None | object = _UNSET,
        tool_name: str | None | object = _UNSET,
        current_text: str | None | object = _UNSET,
        user_interrupt_reminder_injected: bool | object = _UNSET,
        cancellation_reason: str | None | object = _UNSET,
        clear_active_tools: bool = False,
    ) -> None:
        state = cls._states.get(session_id, InterruptibleState())
        changes_activity = phase is not _UNSET or tool_name is not _UNSET
        if state.active_tools_by_run_id and changes_activity and not clear_active_tools:
            raise RuntimeError(
                f"存在并发活动工具时不能直接覆盖执行阶段: session_id={session_id} "
                f"run_ids={list(state.active_tools_by_run_id)}"
            )
        if clear_active_tools:
            state.active_tools_by_run_id.clear()
        if phase is not _UNSET:
            state.phase = phase
        if tool_name is not _UNSET:
            state.tool_name = tool_name
        if current_text is not _UNSET:
            state.current_text = "" if current_text is None else current_text
        if user_interrupt_reminder_injected is not _UNSET:
            state.user_interrupt_reminder_injected = bool(user_interrupt_reminder_injected)
        if cancellation_reason is not _UNSET:
            state.cancellation_reason = cancellation_reason
        cls._states[session_id] = state

    @classmethod
    def start_tool(cls, session_id: str, *, run_id: str, tool_name: str) -> InterruptibleState:
        """登记一个活动工具；同一 run_id 不允许指向不同工具。"""
        if not run_id:
            raise ValueError("登记活动工具时 run_id 不能为空")
        if not tool_name:
            raise ValueError("登记活动工具时 tool_name 不能为空")

        state = cls._states.get(session_id, InterruptibleState())
        existing_tool_name = state.active_tools_by_run_id.get(run_id)
        if existing_tool_name is not None and existing_tool_name != tool_name:
            raise RuntimeError(
                f"活动工具 run_id 冲突: session_id={session_id} run_id={run_id} "
                f"existing={existing_tool_name} incoming={tool_name}"
            )
        state.active_tools_by_run_id[run_id] = tool_name
        state.phase = "tool"
        state.tool_name = cls._summarize_active_tools(state)
        cls._states[session_id] = state
        return state

    @classmethod
    def end_tool(cls, session_id: str, *, run_id: str) -> InterruptibleState:
        """移除已结束工具；其他并发工具仍保持可打断状态。"""
        state = cls._states.get(session_id)
        if state is None or run_id not in state.active_tools_by_run_id:
            raise RuntimeError(
                f"结束了未登记的活动工具: session_id={session_id} run_id={run_id}"
            )

        del state.active_tools_by_run_id[run_id]
        state.tool_name = cls._summarize_active_tools(state)
        state.phase = "tool" if state.active_tools_by_run_id else None
        cls._states[session_id] = state
        return state

    @staticmethod
    def _summarize_active_tools(state: InterruptibleState) -> str | None:
        names = state.active_tool_names
        if not names:
            return None
        return "、".join(names)

    @classmethod
    def clear(cls, session_id: str) -> None:
        cls._states.pop(session_id, None)
