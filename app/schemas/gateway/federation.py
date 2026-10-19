from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.workspace_identity import validate_workspace_id
from app.schemas.gateway.workspaces import GatewayWorkspaceStatus


class FederationProtocolManifestDTO(BaseModel):
    protocol_version: int
    gateway_id: str
    federation_depth: Literal[0] = 0
    capabilities: list[str] = Field(default_factory=list)
    config_event_cursor: int = Field(default=0, ge=0)
    config_reload_state: str | None = None
    config_reload_restart_required: bool = False
    config_reload_candidate_ref: str | None = None


class FederationWorkspaceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    backend_workspace_id: str
    backend_generation: str | None = None
    affected_workspace_ids: list[str] = Field(default_factory=list)
    name: str
    managed: bool
    connection_kind: Literal["local"]
    services: list[Literal["workspace_api", "terminal_manager", "browser_manager"]] = (
        Field(default_factory=list)
    )

    @field_validator("backend_workspace_id")
    @classmethod
    def backend_uuid(cls, value: str) -> str:
        try:
            return validate_workspace_id(value)
        except RuntimeError as error:
            # 将唯一 UUID 校验器的拒绝转成 DTO 字段错误，保留原错误详情。
            raise ValueError(str(error)) from error


class FederationWorkspaceListDTO(BaseModel):
    protocol_version: int
    gateway_id: str
    items: list[FederationWorkspaceDTO] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)


class GatewayInboundPeerDTO(BaseModel):
    connection_id: str
    peer_gateway_id: str
    credential_expires_at: str


class GatewayInboundWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str
    status: GatewayWorkspaceStatus
    managed: bool
    system_default: bool


class GatewayInboundAccessListDTO(BaseModel):
    gateway_id: str
    peers: list[GatewayInboundPeerDTO] = Field(default_factory=list)
    items: list[GatewayInboundWorkspaceDTO] = Field(default_factory=list)
