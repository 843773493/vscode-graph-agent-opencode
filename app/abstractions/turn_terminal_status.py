from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.itemized.records import CanonicalItemRecord


def tool_result_content_text(content: object) -> str:
    """工具事件与 request carrier 共用同一正文文本投影。"""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        return "".join(
            str(block.get("text", block.get("content", "")) if isinstance(block, Mapping) else block)
            for block in content
        )
    return str(content)


@dataclass(frozen=True, slots=True)
class ToolResultCompletion:
    """真实工具 attempt 的终态通知；canonical item 只由持久化 owner 创建。"""

    model_call_id: str
    tool_call_id: str
    tool_invocation_id: str
    tool_attempt_id: str
    tool_name: str
    content: str
    tool_outcome: Literal["success", "failure", "cancelled", "unknown"]

    def __post_init__(self) -> None:
        for name in ("model_call_id", "tool_call_id", "tool_invocation_id", "tool_attempt_id", "tool_name"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"ToolResultCompletion.{name} 必须是真实非空身份")
        if not isinstance(self.content, str):
            raise TypeError("工具终态正文必须是字符串")
        if self.tool_outcome not in {"success", "failure", "cancelled", "unknown"}:
            raise ValueError(f"未知工具终态: {self.tool_outcome}")

    @property
    def item_id(self) -> str:
        return f"item-{self.tool_attempt_id}-result-{self.tool_call_id}"


class TurnTerminalStatusWriter(Protocol):
    """把已准入 execution 的控制终态交给唯一 convergence owner。"""

    def commit_tool_result(
        self, *, session_id: str, thread_id: str, job_id: str,
        completion: ToolResultCompletion, recovery: bool = False,
        checkpoint_ns: str = "",
    ) -> CanonicalItemRecord: ...

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


__all__ = ["ToolResultCompletion", "TurnTerminalStatusWriter", "tool_result_content_text"]
