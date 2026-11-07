"""消费原安装器的精确发布描述，在目标 Python 读取包文件。"""

from __future__ import annotations

from base64 import b64encode
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, JsonValue

from app.core.atomic_fs import native_io_path
from app.schemas.gateway.extension_host import NativeFileURI
from app.services.infrastructure.extension_host.configuration import (
    native_file_errors,
    native_path,
    native_stat,
)
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
    NativeFileRead,
    NativeFileStat,
)


class NativeExtensionPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    native_uri: NativeFileURI
    extensions_root_uri: NativeFileURI


def publication_file(publication: NativeExtensionPublication, *, extension_key: str,
                     relative_segments: JsonValue, file_operation: Literal["stat", "read"]) -> NativeFileStat | NativeFileRead:
    """调用方持有原安装串行准入；不扫描安装目录或维护另一份库存。"""
    if not isinstance(relative_segments, list) or any(
        not isinstance(segment, str) or not segment or segment in {".", ".."}
        or Path(segment).anchor or any(char in segment for char in ("/", "\\", "\0")) for segment in relative_segments
    ):
        raise ExtensionManagementError("扩展文件必须使用有效的包内相对路径段", "NoPermissions")
    segments = cast(list[str], relative_segments)
    with native_file_errors():
        package = native_path(publication.native_uri)
        root = native_path(publication.extensions_root_uri)
        real_root = native_io_path(root).resolve(strict=True)
        real_package = native_io_path(package).resolve(strict=True)
        if package.parent != root or package.name != extension_key or real_package.parent != real_root or real_package.name != extension_key:
            raise ExtensionManagementError("扩展包目录符号链接越出原安装位置", "NoPermissions")
        path = package.joinpath(*segments)
        if not native_io_path(path).resolve(strict=True).is_relative_to(real_package):
            raise ExtensionManagementError("扩展文件符号链接越出已发布包", "NoPermissions")
        metadata = native_stat(path)
        if file_operation == "stat":
            return metadata
        if not metadata.type & 1:
            raise ExtensionManagementError("扩展包读取目标不是普通文件", "EntryIsADirectory")
        # 保留原私有正文上限；限制实际读取，文件增长也不会生成无界控制回执。
        maximum_size = 8 * 1024 * 1024
        with native_io_path(path).open("rb") as stream:
            content = stream.read(maximum_size + 1)
        if len(content) > maximum_size:
            raise ExtensionManagementError(f"扩展包文件超过私有读取上限 {maximum_size} 字节", "EntryTooLarge")
        return NativeFileRead(content_base64=b64encode(content).decode("ascii"))
