"""插件私有输入公开写入引用及认证启动交付合同。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue, Secret, field_serializer

from app.schemas.canonical_params import CanonicalWorkspaceId
from app.schemas.gateway.service_plugin import PluginBinding, PluginTarget
from configs.plugin_parameters import PluginParameters, PluginPrivateInputReference


class PluginPrivateInputWriteRequest(PluginTarget):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    workspace_id: CanonicalWorkspaceId | None = None
    package_id: str = Field(min_length=1)
    plugin_id: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    source_key: str = Field(min_length=1)
    parameter_pointer: str = Field(pattern=r"^(?:/(?:[^~/]|~[01])*)+$")
    value: Secret[JsonValue] = Field(repr=False)


class PluginParameterDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(min_length=1)
    binding: PluginBinding
    configuration_schema: dict[str, JsonValue] | None


class PluginParameterSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    binding: PluginBinding
    configuration_schema: dict[str, JsonValue] | None


class WorkspacePluginSchemas(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    gateway_id: str
    workspace_id: CanonicalWorkspaceId
    plugins: list[PluginParameterSchema]


class PluginParameterDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    values: Secret[PluginParameters] = Field(repr=False)

    @field_serializer("values")
    def serialize_private_delivery(self, value: Secret[PluginParameters]) -> PluginParameters:
        # 只用于认证的内部启动／候选校验响应，不进入普通快照或接管记录。
        return value.get_secret_value()


__all__ = ["PluginParameterDelivery", "PluginParameterDeliveryRequest", "PluginPrivateInputReference",
           "PluginPrivateInputWriteRequest"]
