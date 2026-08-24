"""会话历史加载策略的跨 Gateway/工作区后端共享模型。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

HistoryInclude = Literal[
    "user",
    "text",
    "reasoning_summary",
    "reasoning_detail",
    "encrypted_reasoning_meta",
    "assistant_text",
    "assistant",
    "tool_summary",
    "tool_call",
    "tool_result",
    "thinking",
    "internal",
    "metadata",
    "final_response",
]

DEFAULT_INITIAL_INCLUDE: tuple[str, ...] = (
    "user",
    "reasoning_summary",
    "tool_summary",
    "final_response",
)
DEFAULT_ANCHOR_INCLUDE: tuple[str, ...] = ("user", "final_response")
_VALID_INCLUDES = frozenset(
    {
        "user",
        "text",
        "reasoning_summary",
        "reasoning_detail",
        "encrypted_reasoning_meta",
        "assistant_text",
        "assistant",
        "tool_summary",
        "tool_call",
        "tool_result",
        "thinking",
        "internal",
        "metadata",
        "final_response",
    }
)


@dataclass(frozen=True, slots=True)
class HistoryLoadingConfig:
    """Gateway 所属会话使用的首次加载和锚点窗口策略。"""

    initial_turns: int = 1
    initial_include: tuple[str, ...] = DEFAULT_INITIAL_INCLUDE
    anchor_before_turns: int = 4
    anchor_after_turns: int = 4
    anchor_include: tuple[str, ...] = DEFAULT_ANCHOR_INCLUDE

    def __post_init__(self) -> None:
        if self.initial_turns < 1:
            raise ValueError("历史初始批次必须大于 0")
        if self.anchor_before_turns < 1 or self.anchor_after_turns < 1:
            raise ValueError("锚点两侧 Turn 数必须大于 0")
        _validate_includes(self.initial_include, "initial.include")
        _validate_includes(self.anchor_include, "anchor.include")

    def anchor_limit(self, direction: str) -> int:
        if direction == "before":
            return self.anchor_before_turns
        if direction == "after":
            return self.anchor_after_turns
        raise ValueError(f"锚点窗口方向非法: {direction}")

    def as_dict(self) -> dict[str, object]:
        return {
            "initial_turns": self.initial_turns,
            "initial_include": list(self.initial_include),
            "anchor_before_turns": self.anchor_before_turns,
            "anchor_after_turns": self.anchor_after_turns,
            "anchor_include": list(self.anchor_include),
        }

    def as_header_value(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_mapping(cls, value: object) -> HistoryLoadingConfig:
        if not isinstance(value, dict):
            raise TypeError("历史加载策略必须是对象")
        initial_turns = value.get("initial_turns", 1)
        anchor_before_turns = value.get("anchor_before_turns", 4)
        anchor_after_turns = value.get("anchor_after_turns", 4)
        initial_include = value.get("initial_include", list(DEFAULT_INITIAL_INCLUDE))
        anchor_include = value.get("anchor_include", list(DEFAULT_ANCHOR_INCLUDE))
        if (
            isinstance(initial_turns, bool)
            or not isinstance(initial_turns, int)
            or isinstance(anchor_before_turns, bool)
            or not isinstance(anchor_before_turns, int)
            or isinstance(anchor_after_turns, bool)
            or not isinstance(anchor_after_turns, int)
            or not isinstance(initial_include, list | tuple)
            or not isinstance(anchor_include, list | tuple)
        ):
            raise TypeError("历史加载策略字段类型非法")
        if not all(isinstance(item, str) for item in initial_include):
            raise TypeError("历史初始 include 必须是字符串数组")
        if not all(isinstance(item, str) for item in anchor_include):
            raise TypeError("历史锚点 include 必须是字符串数组")
        return cls(
            initial_turns=initial_turns,
            initial_include=tuple(initial_include),
            anchor_before_turns=anchor_before_turns,
            anchor_after_turns=anchor_after_turns,
            anchor_include=tuple(anchor_include),
        )


def default_history_loading_config() -> HistoryLoadingConfig:
    return HistoryLoadingConfig()


def parse_history_loading_header(value: str | None) -> HistoryLoadingConfig:
    if value is None or not value.strip():
        return default_history_loading_config()
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("Gateway 历史加载策略请求头不是合法 JSON") from error
    return HistoryLoadingConfig.from_mapping(raw)


def _validate_includes(values: tuple[str, ...], field: str) -> None:
    if not values:
        raise ValueError(f"{field} 不能为空")
    unknown = [item for item in values if item not in _VALID_INCLUDES]
    if unknown:
        raise ValueError(f"{field} 包含未知投影字段: {unknown}")
