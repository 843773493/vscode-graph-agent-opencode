"""目标扩展包只读资源；逻辑 URI 与不可变发布选择各自保持职责。"""

from __future__ import annotations

from pydantic import Field, model_validator

from app.core.resource_uri import parse_resource_uri
from app.schemas.gateway.extension_installation import (
    ExtensionInstallationInventoryRequest,
    _InventoryValue,
)


class ExtensionPackageFileRequest(ExtensionInstallationInventoryRequest):
    resource_uri: str = Field(min_length=1)
    extension_key: str = Field(min_length=1)
    generation: str | None = Field(default=None, min_length=1)
    view_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def resource_belongs_to_target(self) -> ExtensionPackageFileRequest:
        resource = parse_resource_uri(self.resource_uri)
        if (resource.scope != "gateway" or resource.scope_id != self.gateway_id or resource.kind != "extensions"
            or resource.gateway_authority != self.gateway_id or resource.tail_segments[0] != self.backend_id):
            raise ValueError("扩展包资源不属于明确 Gateway 和 Backend")
        if (self.generation is None) != (self.view_id is None):
            raise ValueError("运行扩展包必须同时提供原 view 和 generation")
        return self


class ExtensionPackageReadDTO(_InventoryValue):
    resource_uri: str
    extension_key: str
    content_base64: str


class ExtensionPackageStatDTO(_InventoryValue):
    resource_uri: str
    extension_key: str
    type: int
    ctime: float
    mtime: float
    size: int
    permissions: int | None
