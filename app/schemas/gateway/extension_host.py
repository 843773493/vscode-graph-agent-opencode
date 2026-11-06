"""宿主通道边界；公开结构以 Gateway proto 为源，保留内部业务校验。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.canonical_params import CanonicalGatewayId, CanonicalWorkspaceId


class ExtensionHostOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    protocol_version: int = Field(ge=1, le=1)
    gateway_id: CanonicalGatewayId
    backend_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    view_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    workspace_ids: list[CanonicalWorkspaceId] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_workspaces(self) -> ExtensionHostOpenRequest:
        if len(set(self.workspace_ids)) != len(self.workspace_ids):
            raise ValueError("Node 宿主工作区视图不能重复")
        return self


class ExtensionHostConnectionDTO(ExtensionHostOpenRequest):
    client_id: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
