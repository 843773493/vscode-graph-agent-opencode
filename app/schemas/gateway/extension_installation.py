"""目标默认 profile 的原生扩展库存；描述正文保留 Code OSS JSON。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.schemas.gateway.extension_host import BackendId, ExtensionHostTargetRequest


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


class ExtensionInstallationInventoryRequest(ExtensionHostTargetRequest):
    backend_id: BackendId


class ExtensionInstallationInventoryDTO(ExtensionInstallationInventoryRequest):
    profile_scope: Literal["default"] = "default"
    inventory: ProfileExtensionInventory
