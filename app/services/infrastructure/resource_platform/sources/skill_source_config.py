"""代码内登记的 Skill 获取能力与独立更新策略；不读取来源或解释正文。"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    field_validator,
    model_validator,
)

from app.services.infrastructure.resource_platform.observation.source_update_policy import (
    SourceUpdatePolicy,
)
from app.services.infrastructure.resource_platform.sources.http_skill_fetch import (
    HttpSkillFetch,
    HttpSkillFetchConfig,
)
from app.services.infrastructure.resource_platform.sources.skill_fetch_contracts import (
    SkillFetchCapability,
    SkillFetchConfig,
)

SKILL_FETCH_CAPABILITIES: Mapping[str, SkillFetchCapability] = MappingProxyType({
    "http": SkillFetchCapability(config_type=HttpSkillFetchConfig, create=HttpSkillFetch),
})


def skill_fetch_capability(protocol: str) -> SkillFetchCapability:
    """只选择发行代码已登记的能力；配置不能导入代码或自行安装协议。"""
    if protocol not in SKILL_FETCH_CAPABILITIES:
        raise ValueError(f"Skill 获取能力未登记: protocol={protocol}")
    return SKILL_FETCH_CAPABILITIES[protocol]


class SkillSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    fetch: SerializeAsAny[SkillFetchConfig]
    updates: SourceUpdatePolicy = Field(default_factory=lambda: SourceUpdatePolicy(mode="poll", poll_interval="1h"))
    priority: int = Field(default=0, strict=True)

    @field_validator("fetch", mode="before")
    @classmethod
    def parse_registered_fetch(cls, value: object) -> SkillFetchConfig:
        if isinstance(value, SkillFetchConfig):
            capability = skill_fetch_capability(value.protocol)
            if not isinstance(value, capability.config_type):
                raise ValueError("Skill 获取能力与 typed 配置不一致")  # noqa: TRY004 - 配置错误须汇入 Pydantic ValidationError。
            return value
        if not isinstance(value, Mapping) or not isinstance(value.get("protocol"), str):
            raise ValueError("Skill fetch 必须指定已登记的 protocol")  # noqa: TRY004 - 配置错误须汇入 Pydantic ValidationError。
        return skill_fetch_capability(value["protocol"]).config_type.model_validate(value)

    @model_validator(mode="after")
    def require_supported_trigger(self) -> Self:
        self.updates.require_event_capability(skill_fetch_capability(self.fetch.protocol).subscribe is not None)
        return self


def validate_skill_sources(value: object) -> tuple[SkillSourceConfig, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("Skill sources 必须是数组")
    sources = tuple(SkillSourceConfig.model_validate(item) for item in value)
    if len({source.id for source in sources}) != len(sources):
        raise ValueError("Skill source id 重复")
    return sources
