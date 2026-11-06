"""宿主通道边界；公开结构以 Gateway proto 为源，保留内部业务校验。"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

from app.core.config_sources import validate_config_source_uri
from app.core.resource_uri import parse_resource_uri
from app.schemas.canonical_params import CanonicalGatewayId, CanonicalWorkspaceId
from app.schemas.internal_v2.workspace_file_operations import WorkspaceFileURI

BackendId = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]


def _native_file_uri(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "file" or not parsed.path.startswith("/") or parsed.query or parsed.fragment:
        raise ValueError("Node 宿主原生位置必须为目标机器的绝对 file URI")
    return value


NativeFileURI = Annotated[str, AfterValidator(_native_file_uri)]
ExtensionConfigurationURI = Annotated[str, AfterValidator(validate_config_source_uri), PlainSerializer(validate_config_source_uri)]


class _HostValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class ExtensionHostConfigurationResourceDTO(_HostValue):
    native_uri: NativeFileURI
    resource_uri: ExtensionConfigurationURI


class ExtensionHostEnvironmentDTO(_HostValue):
    app_root_uri: NativeFileURI
    global_storage_home_uri: NativeFileURI
    workspace_storage_home_uri: NativeFileURI
    profile_id: str = Field(min_length=1)
    remote_settings: ExtensionHostConfigurationResourceDTO


class ExtensionHostFolderDTO(_HostValue):
    workspace_id: CanonicalWorkspaceId
    resource_uri: WorkspaceFileURI
    native_uri: NativeFileURI
    name: str = Field(min_length=1)


class ExtensionHostWorkspaceDTO(_HostValue):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    folders: list[ExtensionHostFolderDTO] = Field(min_length=1)
    configuration: ExtensionHostConfigurationResourceDTO | None = None


class ExtensionHostInitializationDTO(_HostValue):
    os: Literal["linux", "windows", "macos"]
    parent_pid: int = Field(gt=0)
    environment: ExtensionHostEnvironmentDTO
    logs_location_uri: NativeFileURI
    workspace: ExtensionHostWorkspaceDTO


class ExtensionWorkspaceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    protocol_version: int = Field(ge=1, le=1)
    gateway_id: CanonicalGatewayId
    workspace_ids: list[CanonicalWorkspaceId] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_workspaces(self) -> ExtensionWorkspaceTarget:
        if len(set(self.workspace_ids)) != len(self.workspace_ids):
            raise ValueError("Node 宿主工作区视图不能重复")
        return self


class ExtensionHostTargetRequest(ExtensionWorkspaceTarget):
    backend_id: BackendId | None = None


class ExtensionHostOpenRequest(ExtensionWorkspaceTarget):
    backend_id: BackendId
    view_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class ExtensionHostConnectionDTO(ExtensionHostOpenRequest):
    client_id: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    initialization: ExtensionHostInitializationDTO

    @model_validator(mode="after")
    def initialized_view_matches_binding(self) -> ExtensionHostConnectionDTO:
        initialization = self.initialization
        folders = initialization.workspace.folders
        if [folder.workspace_id for folder in folders] != self.workspace_ids:
            raise ValueError("Node 宿主初始化目录与有序工作区视图不符")
        for folder in folders:
            resource = parse_resource_uri(folder.resource_uri)
            # 原文件 owner 的 display 根不重复编码 Gateway；归属由外层认证 binding 承载。
            if (resource.gateway_authority is not None or resource.scope != "workspace"
                or resource.scope_id != folder.workspace_id or resource.kind != "files" or resource.tail_segments != ("root",)):
                raise ValueError("Node 宿主目录映射不属于原 Gateway 的工作区根")
        resources = [(initialization.environment.remote_settings,
                      ("profiles", initialization.environment.profile_id, "settings"))]
        configuration = initialization.workspace.configuration
        if (len(folders) > 1) != (configuration is not None):
            raise ValueError("Node 宿主必须仅为明确多根视图提供 workspace 配置")
        if configuration is not None:
            resources.append((configuration, ("views", initialization.workspace.id, "workspace")))
        for configuration_resource, tail in resources:
            resource = parse_resource_uri(configuration_resource.resource_uri)
            if (resource.gateway_authority != self.gateway_id or resource.scope != "gateway"
                or resource.scope_id != self.gateway_id or resource.kind != "config"
                or resource.tail_segments != ("extension-host", self.backend_id, *tail)):
                raise ValueError("Node 宿主配置不属于原 Gateway、Backend、profile 或工作区视图")
        return self


class ExtensionHostRuntimeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    vscode_version: str = Field(min_length=1)
    node_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class ExtensionHostTargetDTO(ExtensionWorkspaceTarget):
    backend_id: BackendId
    source: Literal["live"] = "live"
    platform: str = Field(min_length=1)
    channel_state: Literal["available", "unavailable", "draining"] = Field(
        description="当前绑定与平台支持的通道能力；不代表扩展已激活或完整 API 已接入")
    reason: Literal["runtime_missing", "platform_unsupported", "backend_draining"] | None = None
    runtime: ExtensionHostRuntimeDTO | None = None

    @model_validator(mode="after")
    def consistent_availability(self) -> ExtensionHostTargetDTO:
        expected_state = {None: "available", "runtime_missing": "unavailable",
                          "platform_unsupported": "unavailable", "backend_draining": "draining"}[self.reason]
        if self.channel_state != expected_state or (self.channel_state == "available" and self.runtime is None):
            raise ValueError("Node 宿主通道状态与实际运行时能力不符")
        return self
