"""工作区私有 Skill 来源的管理回执；来源正文仍归原 owner。"""

from pydantic import BaseModel, ConfigDict, Field


class SkillSourcesRefreshDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    revision: str = Field(pattern=r"^sha256:jcs:v1:[0-9a-f]{64}$")
    available: bool
