"""目标默认 profile 的原生扩展库存；描述正文保留 Code OSS JSON。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.core.resource_uri import parse_resource_uri
from app.schemas.canonical_params import CanonicalGatewayId, CanonicalWorkspaceId
from app.schemas.gateway.extension_host import BackendId
from app.schemas.internal_v2.workspace_file_operations import WorkspaceFileURI


class _InventoryValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ExtensionValidation(_InventoryValue):
    severity: int = Field(ge=0, le=3)
    message: str


class InstalledExtension(_InventoryValue):
    source: Literal["profile"]
    extension_key: str = Field(min_length=1)
    description: dict[str, JsonValue]
    is_valid: bool
    validations: list[ExtensionValidation]
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    readme_path: str | None = Field(default=None, min_length=1)
    changelog_path: str | None = Field(default=None, min_length=1)


class ProfileExtensionInventory(_InventoryValue):
    profile_id: str = Field(min_length=1)
    extensions: list[InstalledExtension]
    target_platform: str = Field(min_length=1)


class ExtensionInstallationTarget(_InventoryValue):
    protocol_version: int = Field(ge=1, le=1)
    gateway_id: CanonicalGatewayId
    backend_id: BackendId


class ExtensionInstallationInventoryRequest(ExtensionInstallationTarget):
    gateway_connection_id: str | None = Field(default=None, min_length=1)


class NativeExtensionFileRequest(ExtensionInstallationTarget):
    """Gateway 到 Backend 的私有参数；包身份由原生 scanner 核验，不接受任意路径。"""

    profile_id: str = Field(min_length=1)
    extension_key: str = Field(min_length=1)
    relative_segments: list[str]
    extension_id: str = Field(min_length=1)
    current_profile_member: bool
    generation: str | None = Field(default=None, min_length=1)
    view_id: str | None = Field(default=None, min_length=1)


class ExtensionInstallationInventoryDTO(ExtensionInstallationTarget):
    profile_scope: Literal["default"] = "default"
    inventory: ProfileExtensionInventory


class ExtensionInstallationInstallRequest(ExtensionInstallationInventoryRequest):
    workspace_ids: list[CanonicalWorkspaceId] = Field(min_length=1)
    source_uri: WorkspaceFileURI

    @model_validator(mode="after")
    def source_belongs_to_target(self) -> ExtensionInstallationInstallRequest:
        if len(set(self.workspace_ids)) != len(self.workspace_ids):
            raise ValueError("VSIX 来源工作区范围不能重复")
        source = parse_resource_uri(self.source_uri, local_gateway_id=self.gateway_id)
        if (source.gateway_authority is not None or source.scope != "workspace" or source.kind != "files"
            or source.scope_id not in self.workspace_ids or len(source.tail_segments) < 2):
            raise ValueError("VSIX 来源必须是明确目标已挂接工作区的文件 URI")
        return self


class VsixInstallResult(_InventoryValue):
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    reused: bool


class ExtensionInstallationInstallDTO(ExtensionInstallationInventoryDTO):
    workspace_ids: list[CanonicalWorkspaceId] = Field(min_length=1)
    installed: VsixInstallResult


class ExtensionGalleryInstallOptions(_InventoryValue):
    install_pre_release_version: bool | None = None
    install_given_version: bool | None = None
    donot_include_pack_and_dependencies: bool | None = None
    is_machine_scoped: bool | None = None
    is_application_scoped: bool | None = None
    pinned: bool | None = None
    pre_release: bool | None = None


class ExtensionInstallationGalleryRequest(ExtensionInstallationInventoryRequest):
    extension_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    options: ExtensionGalleryInstallOptions


class ExtensionInstallationGalleryDTO(ExtensionInstallationInventoryDTO):
    installed: VsixInstallResult


class ExtensionMetadataPatch(_InventoryValue):
    is_machine_scoped: bool | None = None
    is_application_scoped: bool | None = None
    pinned: bool | None = None
    pre_release: bool | None = None


class _ProfileMemberRequest(ExtensionInstallationInventoryRequest):
    profile_id: str = Field(min_length=1)
    extension_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class ExtensionInstallationUninstallRequest(_ProfileMemberRequest):
    pass


class ExtensionInstallationMetadataRequest(_ProfileMemberRequest):
    metadata: ExtensionMetadataPatch
    refresh_from_gallery: bool = False


class ExtensionUninstallResult(_InventoryValue):
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)


class ExtensionInstallationUninstallDTO(ExtensionInstallationInventoryDTO):
    uninstalled: ExtensionUninstallResult
