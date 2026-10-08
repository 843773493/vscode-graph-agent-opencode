from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GatewayUserLeaseDTO(BaseModel):
    occupied: bool = False
    client_label: str | None = None
    heartbeat_at: str | None = None
    expires_at: str | None = None


class GatewayUserDTO(BaseModel):
    user_id: str
    display_name: str
    created_at: str
    lease: GatewayUserLeaseDTO = Field(default_factory=GatewayUserLeaseDTO)


class GatewayUserListDTO(BaseModel):
    items: list[GatewayUserDTO] = Field(default_factory=list)


class CreateGatewayUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=120)
    user_id: str | None = Field(default=None, min_length=1, max_length=64)


class AcquireGatewayUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_label: str | None = Field(default=None, max_length=120)


class CreateGatewayGuestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracking: dict[str, str | int | bool] = Field(default_factory=dict)


class GatewayUserAccessDTO(BaseModel):
    kind: Literal["user", "guest"]
    user_id: str | None = None
    lease_generation: int
    expires_at: str | None = None
    takeover: bool = False


class GatewayUserViewStateDTO(BaseModel):
    user_id: str
    workspace_id: str
    session_id: str
    turn_anchor: str | None = None
    scroll_offset: float = Field(ge=0)
    follow_latest: bool = False
    projection_version: int = Field(ge=1)
    tool_details_expanded: bool = False
    updated_at: str


class GatewayUserViewStateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    turn_anchor: str | None = None
    scroll_offset: float = Field(default=0, ge=0)
    follow_latest: bool = False
    projection_version: int = Field(default=1, ge=1)
    tool_details_expanded: bool = False
