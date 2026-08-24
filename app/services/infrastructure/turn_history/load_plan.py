"""历史读取的结构化计划模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.core.history_loading import HistoryInclude

LoadDirection = Literal["head", "tail", "before", "after", "around"]


@dataclass(frozen=True, slots=True)
class LoadLimits:
    turns: int = 64
    records: int = 512
    bytes: int = 4 * 1024 * 1024
    chars: int = 256 * 1024
    detail_batch: int = 4
    item_chars: int = 64 * 1024

    def __post_init__(self) -> None:
        if any(
            value < 1
            for value in (
                self.turns,
                self.records,
                self.bytes,
                self.chars,
                self.detail_batch,
                self.item_chars,
            )
        ):
            raise ValueError("历史读取 limits 必须全部为正整数")


@dataclass(slots=True)
class DetailReadBudget:
    """一次 history 请求共享的详情预算。"""

    limits: LoadLimits = field(default_factory=LoadLimits)
    records_used: int = 0
    bytes_used: int = 0
    chars_used: int = 0

    def can_add(self, *, byte_count: int, char_count: int) -> bool:
        return (
            self.records_used < self.limits.records
            and byte_count <= self.limits.item_chars
            and char_count <= self.limits.item_chars
            and self.bytes_used + byte_count <= self.limits.bytes
            and self.chars_used + char_count <= self.limits.chars
        )

    def add(self, *, byte_count: int, char_count: int) -> None:
        if not self.can_add(byte_count=byte_count, char_count=char_count):
            raise ValueError("详情读取预算已耗尽")
        self.records_used += 1
        self.bytes_used += byte_count
        self.chars_used += char_count


@dataclass(frozen=True, slots=True)
class LoadPlan:
    direction: LoadDirection
    cursor: str | None = None
    turns: int = 1
    include: tuple[HistoryInclude, ...] = (
        "user",
        "thinking",
        "tool_summary",
        "final_response",
    )
    before_turns: int | None = None
    after_turns: int | None = None
    limits: LoadLimits = LoadLimits()

    def __post_init__(self) -> None:
        if self.turns < 1:
            raise ValueError("LoadPlan.turns 必须大于 0")
        if self.turns > self.limits.turns:
            raise ValueError("LoadPlan.turns 超过服务端历史 Turn 上限")
        if self.direction == "around":
            if self.before_turns is None or self.after_turns is None:
                raise ValueError("around LoadPlan 必须包含两侧 Turn 数量")
        elif self.before_turns is not None or self.after_turns is not None:
            raise ValueError("只有 around LoadPlan 可以包含两侧 Turn 数量")
