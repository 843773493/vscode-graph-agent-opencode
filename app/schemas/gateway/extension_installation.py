"""目标默认 profile 的原生扩展库存；描述正文保留 Code OSS JSON。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.core.resource_uri import parse_resource_uri
from app.schemas.gateway.extension_host import BackendId, ExtensionWorkspaceTarget
from app.schemas.internal_v2.workspace_file_operations import WorkspaceFileURI


class _InventoryValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ExtensionValidation(_InventoryValue):
    severity: int = Field(ge=0, le=3)
    message: str


class InstalledExtension(_InventoryValue):
    source: Literal["profile"]
    description: dict[str, JsonValue]
    is_valid: bool
    validations: list[ExtensionValidation]


class ProfileExtensionInventory(_InventoryValue):
    profile_id: str = Field(min_length=1)
    extensions: list[InstalledExtension]


class ExtensionInstallationInventoryRequest(ExtensionWorkspaceTarget):
    backend_id: BackendId


class ExtensionInstallationInventoryDTO(ExtensionInstallationInventoryRequest):
    profile_scope: Literal["default"] = "default"
    inventory: ProfileExtensionInventory


class ExtensionInstallationInstallRequest(ExtensionInstallationInventoryRequest):
    source_uri: WorkspaceFileURI

    @model_validator(mode="after")
    def source_belongs_to_target(self) -> ExtensionInstallationInstallRequest:
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
    installed: VsixInstallResult
