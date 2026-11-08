"""Workspace owner 返回给本机 Gateway 的已提交插件绑定快照。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.gateway.service_plugin import PluginBinding


class WorkspacePluginSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    workspace_id: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    backend_generation: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    bindings: list[PluginBinding]
    referenced_bindings: list[PluginBinding]

    @model_validator(mode="after")
    def bindings_match_workspace(self) -> WorkspacePluginSnapshot:
        for binding in (*self.bindings, *self.referenced_bindings):
            if binding.workspace_id != self.workspace_id:
                raise ValueError("Workspace 插件绑定归属与快照 workspace_id 不一致")
        return self


__all__ = ["WorkspacePluginSnapshot"]
