from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class InterruptibleState:
    """某个 session 当前可打断的执行状态。"""

    phase: str | None = None
    tool_name: str | None = None
    current_text: str = ""


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
        phase: str | None = None,
        tool_name: str | None = None,
        current_text: str | None = None,
    ) -> None:
        state = cls._states.get(session_id, InterruptibleState())
        if phase is not None:
            state.phase = phase
        if tool_name is not None:
            state.tool_name = tool_name
        if current_text is not None:
            state.current_text = current_text
        cls._states[session_id] = state

    @classmethod
    def clear(cls, session_id: str) -> None:
        cls._states.pop(session_id, None)
