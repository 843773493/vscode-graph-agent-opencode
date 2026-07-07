from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Final


_UNSET: Final = object()


@dataclass
class InterruptibleState:
    """某个 session 当前可打断的执行状态。"""

    phase: str | None = None
    tool_name: str | None = None
    current_text: str = ""
    user_interrupt_reminder_injected: bool = False


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
    ) -> None:
        state = cls._states.get(session_id, InterruptibleState())
        if phase is not _UNSET:
            state.phase = phase
        if tool_name is not _UNSET:
            state.tool_name = tool_name
        if current_text is not _UNSET:
            state.current_text = "" if current_text is None else current_text
        if user_interrupt_reminder_injected is not _UNSET:
            state.user_interrupt_reminder_injected = bool(user_interrupt_reminder_injected)
        cls._states[session_id] = state

    @classmethod
    def clear(cls, session_id: str) -> None:
        cls._states.pop(session_id, None)
