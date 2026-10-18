"""Gateway 自有 Skill 原始来源快照；不携带宿主路径或凭据。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GatewaySkillSourceEntryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    entry_identity: str
    source_id: str
    display_uri: str
    revision: str | None
    byte_length: int = Field(ge=0)
    content: str
    available: bool
    error_code: Literal["source-unavailable"] | None = None


class GatewaySkillSourcesDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gateway_id: str
    workspace_id: str
    revision: str
    entries: tuple[GatewaySkillSourceEntryDTO, ...]
    available: bool
    error_code: Literal["source-unavailable"] | None = None
