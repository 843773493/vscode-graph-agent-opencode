from __future__ import annotations

from typing import Protocol


class TurnTerminalStatusWriter(Protocol):
    """把持久化 Turn 的终态写入端口，供启动恢复与 Job 终态同步共用。"""

    def mark_turn_terminal_status(
        self,
        *,
        session_id: str,
        turn_id: str,
        status: str,
    ) -> bool: ...


__all__ = ["TurnTerminalStatusWriter"]
