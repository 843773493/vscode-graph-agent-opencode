"""自有来源与唯一目标安装操作；运行绑定仍归原配置 owner。"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.gateway.service_plugin import (
    InstalledPluginPackageDTO,
    PluginPackageTarget,
    PluginTarget,
)


class _InstallationValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def validate_download_url(value: str) -> str:
    """来源地址只使用 HTTP(S)，凭据沿目标现有保护规则独立管理。"""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("插件来源地址必须是不含用户凭据或片段的 HTTP(S) URL")
    return value


class PluginSourceConfig(_InstallationValue):
    source_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    protocol_version: Literal[1] = 1
    base_url: str | None = None
    enabled: bool = False

    @model_validator(mode="after")
    def source_address(self) -> PluginSourceConfig:
        if self.base_url is not None:
            validate_download_url(self.base_url)
            if urlsplit(self.base_url).query:
                raise ValueError("插件来源 base_url 不接受查询参数")
        if self.enabled and self.base_url is None:
            raise ValueError("已启用插件来源必须提供 base_url")
        return self


class PluginSourceStatusDTO(_InstallationValue):
    configured: bool
    message: str
    entries: list[PluginSourceConfig]


class PluginInventoryResultDTO(_InstallationValue):
    packages: list[InstalledPluginPackageDTO]
    sources: PluginSourceStatusDTO


class PluginSourceArtifact(_InstallationValue):
    package_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    package_version: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")
    target: PluginPackageTarget | None = None
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    url: str = Field(min_length=1)
    filename: str = Field(min_length=1, pattern=r"^[^/\\]+\.zip$")

    @model_validator(mode="after")
    def artifact_address(self) -> PluginSourceArtifact:
        validate_download_url(self.url)
        return self


class PluginSourceCatalogRequest(PluginTarget):
    source_id: str = Field(min_length=1)
    query: str = ""
    cursor: str | None = None


class PluginSourceCatalogDTO(_InstallationValue):
    gateway_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    protocol_version: Literal[1]
    catalog_revision: str = Field(min_length=1)
    items: list[PluginSourceArtifact]
    next_cursor: str | None = None


class InstallPluginRequest(PluginTarget):
    operation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    path: str = ""
    development_directory: bool = False
    expected_digest: str | None = None
    source_id: str | None = None
    artifact: PluginSourceArtifact | None = None
    catalog_revision: str | None = None

    @model_validator(mode="after")
    def one_acquisition(self) -> InstallPluginRequest:
        if self.source_id is not None:
            if self.artifact is None or not self.catalog_revision or self.path or self.development_directory or self.expected_digest is not None:
                raise ValueError("来源安装必须使用完整目录制品及其版本，不能混入本机路径参数")
        elif not self.path or self.artifact is not None or self.catalog_revision is not None:
            raise ValueError("本地安装必须指定目标机器的原生路径")
        return self


class FrozenPluginInstallArtifact(_InstallationValue):
    source_kind: Literal["local_zip", "development_directory", "source"]
    package_id: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    target: PluginPackageTarget | None = None
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    source_id: str | None = None
    catalog_revision: str | None = None
    path: str | None = None
    filename: str | None = None


class PluginInstallOperationRequest(PluginTarget):
    operation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class PluginInstallOperationDTO(_InstallationValue):
    gateway_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    status: Literal["pending", "running", "succeeded", "failed", "cancelled", "unknown"]
    artifact: FrozenPluginInstallArtifact
    cancel_requested: bool = False
    error: str | None = None
