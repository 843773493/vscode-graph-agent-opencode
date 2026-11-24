"""上下文压缩的不可变策略与容量；不读取模型、配置来源或执行状态。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompactionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prepare_fraction: float = Field(default=0.70, gt=0, lt=1)
    apply_fraction: float = Field(default=0.85, gt=0, lt=1)
    target_fraction: float = Field(default=0.55, gt=0, lt=1)
    apply_policy: Literal["on_demand", "next_request"] = "on_demand"

    @model_validator(mode="after")
    def validate_threshold_order(self) -> Self:
        if not self.target_fraction < self.prepare_fraction < self.apply_fraction:
            raise ValueError(
                "context.compaction 必须满足 0 < target_fraction < prepare_fraction < apply_fraction < 1"
            )
        return self


class ModelCapacity(BaseModel):
    """I、C 和真实输出预留 R；未知值保留为 None，不代表零。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_input_tokens: int | None = Field(default=None, gt=0, strict=True)
    context_window: int | None = Field(default=None, gt=0, strict=True)
    output_reservation_tokens: int | None = Field(default=None, gt=0, strict=True)

    @property
    def input_budget_tokens(self) -> int | None:
        """R 未知时只返回比例参照，不能据此声称输入输出合计已适配。"""
        window = self.context_window
        if window is not None and self.output_reservation_tokens is not None:
            window -= self.output_reservation_tokens
        known = [value for value in (self.max_input_tokens, window) if value is not None]
        return min(known) if known else None

    @property
    def capacity_known(self) -> bool:
        return self.max_input_tokens is not None or self.context_window is not None

    @property
    def output_reservation_known(self) -> bool:
        return self.output_reservation_tokens is not None

    @property
    def cannot_fit(self) -> bool:
        budget = self.input_budget_tokens
        return budget is not None and budget <= 0
