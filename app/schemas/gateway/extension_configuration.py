"""原生配置文件的精确资源传输，不承载业务 JSONC candidate。"""

from __future__ import annotations

from base64 import b64decode
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.gateway.extension_host import (
    ExtensionConfigurationURI,
    ExtensionHostOpenRequest,
    _HostValue,
)


class ExtensionConfigurationViewRequest(ExtensionHostOpenRequest):
    generation: str = Field(min_length=1)


class ExtensionConfigurationFileRequest(ExtensionConfigurationViewRequest):
    resource_uri: ExtensionConfigurationURI


class ExtensionConfigurationWriteOptions(_HostValue):
    create: bool
    overwrite: bool
    unlock: bool
    atomic: bool
    append: bool


class ExtensionConfigurationWriteRequest(ExtensionConfigurationFileRequest):
    content_base64: str
    options: ExtensionConfigurationWriteOptions

    @field_validator("content_base64")
    @classmethod
    def valid_base64(cls, value: str) -> str:
        b64decode(value, validate=True)
        return value


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
