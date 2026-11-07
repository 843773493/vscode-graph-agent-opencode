"""Web 宿主的目标准入与冻结安装快照；不包含 Node 进程初始化。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.canonical_params import CanonicalGatewayId, CanonicalWorkspaceId
from app.schemas.gateway.extension_host import BackendId
from app.schemas.gateway.extension_installation import ProfileExtensionInventory


class _WebHostBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    protocol_version: int = Field(ge=1, le=1)
    gateway_id: CanonicalGatewayId
    backend_id: BackendId
    view_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    workspace_ids: list[CanonicalWorkspaceId]
    profile_id: str = Field(min_length=1)
    extension_keys: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_view_members(self) -> _WebHostBinding:
        if len(set(self.workspace_ids)) != len(self.workspace_ids):
            raise ValueError("Web 宿主工作区视图不能重复")
        if len(set(self.extension_keys)) != len(self.extension_keys):
            raise ValueError("Web 宿主扩展集合不能重复")
        return self


class ExtensionWebHostOpenRequest(_WebHostBinding):
    gateway_connection_id: str | None = Field(default=None, min_length=1)


class ExtensionWebHostConnectionDTO(_WebHostBinding):
    client_id: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    inventory: ProfileExtensionInventory

    @model_validator(mode="after")
    def snapshot_matches_binding(self) -> ExtensionWebHostConnectionDTO:
        if (self.inventory.profile_id != self.profile_id
            or [entry.extension_key for entry in self.inventory.extensions] != self.extension_keys
            or any(not entry.is_valid for entry in self.inventory.extensions)):
            raise ValueError("Web 宿主快照与冻结 profile 和有效扩展集合不符")
        return self
