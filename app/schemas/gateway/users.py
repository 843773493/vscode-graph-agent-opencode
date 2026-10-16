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
    """视图状态键为 `(user_id, workspace_id, session_id)`，只用于 Session 主聊天 main thread；child thread 使用独立视图，新 Session（含 fork）不继承来源状态。无锚点时 writer 记录当前 `scrollTop`，恢复忽略该像素偏移并回到最新尾部。"""

    user_id: str
    workspace_id: str
    session_id: str
    turn_anchor: str | None = Field(
        default=None,
        description="当前 session_id 范围内的锚点 Turn ID；空值或空字符串表示无具体锚点，Web 恢复时回到最新尾部。",
    )
    scroll_offset: float = Field(
        ge=0,
        description="锚点 Turn 顶部被视口上沿裁切的非负距离，单位为 CSS 像素；仅在锚点非空且不跟随最新时用于恢复。",
    )
    follow_latest: bool = Field(
        default=False,
        description="为 true 时恢复到会话最新尾部；否则有锚点时按锚点和偏移恢复，无锚点时仍回到最新尾部。",
    )
    projection_version: int = Field(ge=1)
    tool_details_expanded: bool = False
    updated_at: str


class GatewayUserViewStateUpdateRequest(BaseModel):
    """PUT 用的视图状态完整替换请求；省略字段采用各字段的默认值。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    turn_anchor: str | None = Field(
        default=None,
        description="当前 session_id 范围内的锚点 Turn ID；省略或 null 会清除锚点，空字符串在 Web 恢复时也视为无锚点。",
    )
    scroll_offset: float = Field(
        default=0,
        ge=0,
        description="锚点 Turn 顶部被视口上沿裁切的非负距离，单位为 CSS 像素；仅在锚点非空且不跟随最新时用于恢复。",
    )
    follow_latest: bool = Field(
        default=False,
        description="为 true 时恢复到会话最新尾部；否则有锚点时按锚点和偏移恢复，无锚点时仍回到最新尾部。",
    )
    projection_version: int = Field(default=1, ge=1)
    tool_details_expanded: bool = False
