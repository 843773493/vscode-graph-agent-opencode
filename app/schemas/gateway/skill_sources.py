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


class GatewaySkillPublicationRequest(BaseModel):
    """游标仅标识观察位置；重启或缺口都回到同一个快照接口对账。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    after_cursor: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}:[1-9][0-9]{0,19}$")
    wait_ms: int = Field(default=4000, strict=True, ge=1, le=4000)


class GatewaySkillPublicationDTO(BaseModel):
    """发布后轻量通知，禁止承载正文、私有地址或凭据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gateway_id: str
    workspace_id: str
    cursor: str = Field(pattern=r"^[0-9a-f]{32}:[1-9][0-9]{0,19}$")
    revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    available: bool
    changed: bool
    gap: bool


class GatewaySkillRefreshDTO(BaseModel):
    """本机管理刷新仅返回发布位置，不公开来源正文或私有 locator。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gateway_id: str
    revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    available: bool
