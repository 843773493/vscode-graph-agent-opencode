"""工作区本地与配置 Skill 的 metadata 和管理回执；正文仍归原 owner。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.internal_v2.resource_navigation import (
    NavigationResourceURI,
    ResourceNavigationNodeDTO,
)


class SkillSourcesRefreshDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    revision: str = Field(pattern=r"^sha256:jcs:v1:[0-9a-f]{64}$")
    available: bool


class SkillSourceMetadataDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    display_uri: NavigationResourceURI
    available: bool
    error_code: Literal["source-unavailable"] | None = None
    priority: int


class SkillSourcesMetadataDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    entries: tuple[SkillSourceMetadataDTO, ...]


class LocalSkillMetadataDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    node: ResourceNavigationNodeDTO


class LocalSkillsMetadataDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    entries: tuple[LocalSkillMetadataDTO, ...]
