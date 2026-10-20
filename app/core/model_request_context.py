"""摘要候选的逻辑调用身份；与最终模型任务及 Provider run ID 分离。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage


@dataclass(frozen=True, slots=True)
class SummaryCandidateScope:
    candidate_ordinal: int
    retry_phase: Literal["initial", "tool_free", "visible_text"]
    tools_enabled: bool
    minimal_system: bool
    retry_source_message_ids: tuple[str, ...] = ()
    # 原始完整输入只供 canonical 边界结算，不是摘要请求正文或持久身份。
    canonical_messages: tuple[BaseMessage, ...] = field(kw_only=True, repr=False)

    def __post_init__(self) -> None:
        if type(self.candidate_ordinal) is not int or self.candidate_ordinal < 0:
            raise ValueError("摘要候选序号必须是非负整数")
        if self.retry_phase not in {"initial", "tool_free", "visible_text"}:
            raise ValueError("摘要候选重试阶段非法")
        if type(self.tools_enabled) is not bool or type(self.minimal_system) is not bool:
            raise TypeError("摘要候选的工具和系统提示选择必须为 bool")
        if (not isinstance(self.retry_source_message_ids, tuple)
            or any(not isinstance(value, str) or not value for value in self.retry_source_message_ids)
            or len(set(self.retry_source_message_ids)) != len(self.retry_source_message_ids)):
            raise ValueError("摘要重试来源必须是唯一的消息 ID 元组")

    def identity(self) -> dict[str, object]:
        return {
            "candidate_ordinal": self.candidate_ordinal,
            "retry_phase": self.retry_phase,
            "tools_enabled": self.tools_enabled,
            "minimal_system": self.minimal_system,
            "retry_source_message_ids": self.retry_source_message_ids,
        }


_CURRENT_SUMMARY_CANDIDATE: ContextVar[SummaryCandidateScope | None] = ContextVar(
    "boxteam_summary_candidate", default=None,
)


def current_summary_candidate() -> SummaryCandidateScope | None:
    return _CURRENT_SUMMARY_CANDIDATE.get()


@contextmanager
def summary_candidate_scope(scope: SummaryCandidateScope) -> Iterator[None]:
    token = _CURRENT_SUMMARY_CANDIDATE.set(scope)
    try:
        yield
    finally:
        _CURRENT_SUMMARY_CANDIDATE.reset(token)
