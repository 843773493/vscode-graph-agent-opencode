from __future__ import annotations

from typing import Protocol


class TurnTerminalStatusWriter(Protocol):
    """把已准入 execution 的控制终态交给唯一 convergence owner。"""

    def mark_job_terminal_status(
        self,
        *,
        session_id: str,
        thread_id: str,
        job_id: str,
        status: str,
    ) -> bool: ...

    def mark_turn_terminal_status(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        status: str,
    ) -> bool: ...


__all__ = ["TurnTerminalStatusWriter"]
