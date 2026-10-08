from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.gateway.workspaces import GatewayWorkspaceListDTO


class GatewayRuntimeGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_backend_generation: str | None = Field(default=None, min_length=1)
    expected_workspace_ids: list[str] | None = Field(default=None, min_length=1)
    desired_workspace_ids: list[str] | None = None

    @model_validator(mode="after")
    def complete_group_contract(self) -> GatewayRuntimeGroupRequest:
        if (self.expected_backend_generation is None) != (self.expected_workspace_ids is None):
            raise ValueError("必须同时提供 expected_backend_generation 和完整 expected_workspace_ids")
        for values in (self.expected_workspace_ids, self.desired_workspace_ids):
            if values is not None and len(values) != len(set(values)):
                raise ValueError("Backend 完整成员集合不得包含重复 Gateway target ID")
        if self.desired_workspace_ids is not None and self.expected_backend_generation is None:
            raise ValueError("成员变更必须提交完整组前置条件")
        return self


class GatewayRuntimeBlockerDTO(BaseModel):
    kind: Literal[
        "backend_group",
        "job",
        "tool",
        "background_task",
        "proxy_request",
        "proxy_stream",
    ]
    workspace_id: str
    resource_id: str
    session_id: str
    status: str
    detail: str | None = None


class GatewayRuntimeRestartResultDTO(BaseModel):
    workspace_id: str
    backend_generation: str | None = None
    affected_workspace_ids: list[str] = Field(default_factory=list)
    status: Literal["restarted", "stopped", "blocked"]
    forced: bool
    blockers: list[GatewayRuntimeBlockerDTO] = Field(default_factory=list)
    workspaces: GatewayWorkspaceListDTO


class GatewayRuntimeStateResultDTO(BaseModel):
    workspace_id: str
    backend_generation: str | None = None
    affected_workspace_ids: list[str] = Field(default_factory=list)
    status: Literal["started", "stopped", "blocked"]
    blockers: list[GatewayRuntimeBlockerDTO] = Field(default_factory=list)
    workspaces: GatewayWorkspaceListDTO
