"""Workspace owner 返回给本机 Gateway 的已提交插件绑定快照。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.gateway.plugin_private_inputs import PluginParameterDelivery
from app.schemas.gateway.service_plugin import PluginBinding
from configs.plugin_parameters import PluginParameters


class WorkspacePluginCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    bindings: list[PluginBinding]
    parameters: PluginParameters = Field(default_factory=dict)


class WorkspacePluginValidation(WorkspacePluginCandidate):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    delivery: PluginParameterDelivery


class WorkspacePluginCandidateApply(WorkspacePluginValidation):
    """原 Workspace claim 内对一个插件执行一次性参数替换。"""

    expected_revision: str = Field(min_length=1)
    candidate_revision: str = Field(min_length=1)
    expected_binding: PluginBinding | None = None
    binding: PluginBinding

    @model_validator(mode="after")
    def candidate_binding_is_current(self) -> WorkspacePluginCandidateApply:
        if self.binding not in self.bindings:
            raise ValueError("插件参数替换必须绑定当前候选中的精确插件")
        return self


class WorkspacePluginCreation(BaseModel):
    """创建 owner 冻结的非秘密模板；交接后由工作区原配置接替引用。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    intent_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    bindings: list[PluginBinding]

    @model_validator(mode="after")
    def template_has_no_workspace(self) -> WorkspacePluginCreation:
        keys = [(binding.package_id, binding.plugin_id) for binding in self.bindings]
        if len(keys) != len(set(keys)) or any(binding.workspace_id is not None for binding in self.bindings):
            raise ValueError("创建模板必须是唯一且不含 workspace_id 的插件绑定")
        return self


class WorkspacePluginSnapshot(WorkspacePluginCandidate):

    workspace_id: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    backend_generation: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    referenced_bindings: list[PluginBinding]

    @model_validator(mode="after")
    def bindings_match_workspace(self) -> WorkspacePluginSnapshot:
        for binding in (*self.bindings, *self.referenced_bindings):
            if binding.workspace_id != self.workspace_id:
                raise ValueError("Workspace 插件绑定归属与快照 workspace_id 不一致")
        return self


__all__ = ["WorkspacePluginCandidate", "WorkspacePluginCandidateApply", "WorkspacePluginCreation", "WorkspacePluginSnapshot"]
