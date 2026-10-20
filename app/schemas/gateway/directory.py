from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from app.core.resource_uri import parse_resource_uri


def _directory_uri(value: str) -> str:
    parsed = parse_resource_uri(value)
    if parsed.scope != "gateway" or parsed.kind != "files" or parsed.gateway_authority is not None:
        raise ValueError("目录选择回执必须是原生 Gateway 目录 URI")
    return parsed.display_uri


DirectoryUri = Annotated[str, AfterValidator(_directory_uri)]


class GatewayDirectoryEntryDTO(BaseModel):
    name: str
    path: DirectoryUri = Field(description="目标 Gateway 的原生目录资源 URI")


class GatewayDirectoryListDTO(BaseModel):
    name: str
    path: DirectoryUri = Field(description="当前目录 URI；原生路径只作为管理请求输入")
    parent_path: DirectoryUri | None = None
    home_path: DirectoryUri
    entries: list[GatewayDirectoryEntryDTO] = Field(default_factory=list)
    truncated: bool = False
    limit: int
