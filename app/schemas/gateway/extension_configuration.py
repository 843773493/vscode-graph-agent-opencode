"""原生配置文件的精确资源传输，不承载业务 JSONC candidate。"""

from __future__ import annotations

from base64 import b64decode
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from app.schemas.gateway.extension_host import (
    BackendId,
    CanonicalGatewayId,
    ExtensionConfigurationURI,
    ExtensionHostConfigurationResourceDTO,
    ExtensionHostWorkspaceDTO,
    _ExtensionHostViewRequest,
    _HostValue,
    validate_extension_view,
)


class ExtensionConfigurationTargetRequest(_ExtensionHostViewRequest):
    gateway_connection_id: str | None = Field(default=None, min_length=1)


class ExtensionConfigurationTargetListRequest(_HostValue):
    protocol_version: Annotated[int, Field(ge=1, le=1)]
    gateway_id: CanonicalGatewayId
    gateway_connection_id: str | None = Field(default=None, min_length=1)


class ExtensionConfigurationTargetListDTO(_HostValue):
    protocol_version: Annotated[int, Field(ge=1, le=1)]
    gateway_id: CanonicalGatewayId
    backend_ids: list[BackendId]

    @model_validator(mode="after")
    def unique_backend_ids(self) -> ExtensionConfigurationTargetListDTO:
        if len(self.backend_ids) != len(set(self.backend_ids)):
            raise ValueError("配置目标目录包含重复 Backend 身份")
        return self


class ExtensionConfigurationViewRequest(ExtensionConfigurationTargetRequest):
    generation: str = Field(min_length=1)


class ExtensionConfigurationProfileViewRequest(ExtensionConfigurationTargetRequest):
    profile_id: str = Field(min_length=1)


class ExtensionWebHostContextDTO(_HostValue):
    os: Literal["linux", "windows", "macos"]
    remote_settings: ExtensionHostConfigurationResourceDTO
    workspace: ExtensionHostWorkspaceDTO


class ExtensionConfigurationContributionDTO(_HostValue):
    """原 scanner 已本地化的配置声明；不披露运行入口、包位置或安装元数据。"""

    extension_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    display_name: str | None = None
    configuration: JsonValue = None
    configuration_defaults: dict[str, JsonValue] | None = None
    capabilities: dict[str, JsonValue] | None = None
    enabled_api_proposals: list[str]


class ExtensionConfigurationViewDTO(_ExtensionHostViewRequest):
    profile_id: str = Field(min_length=1)
    context: ExtensionWebHostContextDTO
    extensions: list[ExtensionConfigurationContributionDTO]

    @model_validator(mode="after")
    def context_matches_target(self) -> ExtensionConfigurationViewDTO:
        validate_extension_view(gateway_id=self.gateway_id, backend_id=self.backend_id, workspace_ids=self.workspace_ids,
            workspace=self.context.workspace, profile_id=self.profile_id, settings=self.context.remote_settings)
        identifiers = [extension.extension_id.lower() for extension in self.extensions]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("配置声明包含重复扩展身份")
        return self


class _ConfigurationResource(_HostValue):
    resource_uri: ExtensionConfigurationURI


class ExtensionConfigurationFileRequest(ExtensionConfigurationViewRequest, _ConfigurationResource):
    pass


class ExtensionConfigurationProfileFileRequest(ExtensionConfigurationProfileViewRequest, _ConfigurationResource):
    pass


class ExtensionConfigurationWriteOptions(_HostValue):
    create: bool
    overwrite: bool
    unlock: bool
    atomic: bool
    append: bool


class _ConfigurationContent(_HostValue):
    content_base64: str
    options: ExtensionConfigurationWriteOptions

    @field_validator("content_base64")
    @classmethod
    def valid_base64(cls, value: str) -> str:
        b64decode(value, validate=True)
        return value


class ExtensionConfigurationWriteRequest(ExtensionConfigurationFileRequest, _ConfigurationContent):
    pass


class ExtensionConfigurationProfileWriteRequest(ExtensionConfigurationProfileFileRequest, _ConfigurationContent):
    pass


class ExtensionConfigurationStatDTO(_HostValue):
    resource_uri: ExtensionConfigurationURI
    type: int = Field(ge=1)
    ctime: float
    mtime: float
    size: int = Field(ge=0)
    permissions: int | None = None


class ExtensionConfigurationReadDTO(_HostValue):
    resource_uri: ExtensionConfigurationURI
    content_base64: str


class ExtensionConfigurationChangeDTO(_HostValue):
    resource_uri: ExtensionConfigurationURI
    kind: Literal["create", "edit", "delete"]


class ExtensionConfigurationEventDTO(_HostValue):
    changes: list[ExtensionConfigurationChangeDTO]
    overflow: bool
    error: str | None = None
