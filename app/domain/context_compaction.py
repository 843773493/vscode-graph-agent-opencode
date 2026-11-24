"""上下文压缩的不可变策略；不读取模型、配置来源或执行状态。"""

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
