"""代码内登记的 Skill 获取能力与独立更新策略；不读取来源或解释正文。"""

from __future__ import annotations

from typing import Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.infrastructure.resource_platform.observation.source_update_policy import (
    SourceUpdatePolicy,
)


class HttpSkillFetchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    protocol: Literal["http"]
    url: str = Field(min_length=1)
    timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)

    @field_validator("url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        try:
            parsed = httpx.URL(value)
        except httpx.InvalidURL as error:
            raise ValueError("HTTP Skill 获取地址不是有效 URL") from error
        if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.fragment:
            raise ValueError("HTTP Skill 获取地址必须是无 fragment 的绝对 HTTP(S) URL")
        return value


class SkillSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    fetch: HttpSkillFetchConfig
    updates: SourceUpdatePolicy = Field(default_factory=lambda: SourceUpdatePolicy(mode="poll", poll_interval="1h"))
    priority: int = Field(default=0, strict=True)

    @model_validator(mode="after")
    def require_supported_trigger(self) -> Self:
        if self.updates.mode not in {"poll", "manual"}:
            raise ValueError("HTTP 获取能力尚未登记事件协议；只支持 poll/manual，不自动回退")
        return self


def validate_skill_sources(value: object) -> tuple[SkillSourceConfig, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("Skill sources 必须是数组")
    sources = tuple(SkillSourceConfig.model_validate(item) for item in value)
    if len({source.id for source in sources}) != len(sources):
        raise ValueError("Skill source id 重复")
    return sources
