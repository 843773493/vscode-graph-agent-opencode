"""上下文压缩的不可变策略与容量；不读取模型、配置来源或执行状态。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompactionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prepare_fraction: float = Field(default=0.80, gt=0, lt=1)
    apply_fraction: float = Field(default=0.90, gt=0, lt=1)
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


class InputTokenEstimate(BaseModel):
    """完整计数和已知部分分开；未知媒体不能被当作零开销。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    known_tokens: int = Field(ge=0, strict=True)
    unknown_parts: tuple[str, ...] = ()
    source: str

    @property
    def input_tokens(self) -> int | None:
        return None if self.unknown_parts else self.known_tokens

    @property
    def quality(self) -> Literal["estimated", "unknown"]:
        return "unknown" if self.unknown_parts else "estimated"


class ContextBudget(BaseModel):
    """只读准备证据；策略判断不修改 view，也不声明上游精确适配。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capacity: ModelCapacity
    estimate: InputTokenEstimate

    @property
    def exceeds_capacity(self) -> bool:
        budget = self.capacity.input_budget_tokens
        return budget is not None and (budget <= 0 or self.estimate.known_tokens > budget)

    @property
    def input_fraction(self) -> float | None:
        budget = self.capacity.input_budget_tokens
        tokens = self.estimate.input_tokens
        return tokens / budget if tokens is not None and budget is not None and budget > 0 else None

    def decision(
        self, policy: CompactionPolicy, *, auto_summarize: bool,
    ) -> Literal["unknown", "continue", "prepare", "compact", "cannot_fit"]:
        budget = self.capacity.input_budget_tokens
        if self.exceeds_capacity:
            return "compact" if auto_summarize and budget > 0 else "cannot_fit"
        fraction = self.input_fraction
        if fraction is None:
            return "unknown"
        if auto_summarize:
            if fraction >= policy.apply_fraction:
                return "compact"
            if fraction >= policy.prepare_fraction:
                return "prepare"
        return "continue"


class ContextCapacityError(ValueError):
    """本地容量拒绝，不能归因于上游服务失败。"""

    def __init__(self, budget: ContextBudget, *, apply_fraction: float | None = None) -> None:
        self.budget = budget
        reason = ("完整 Provider 输入超出本次模型预算" if apply_fraction is None
                  else f"完整请求没有满足压缩应用阈值 {apply_fraction:g}")
        super().__init__(
            f"context-capacity-exceeded: {reason}; "
            f"input_estimate={budget.estimate.known_tokens}, "
            f"input_budget={budget.capacity.input_budget_tokens}, "
            f"count_quality={budget.estimate.quality}"
        )
