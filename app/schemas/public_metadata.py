"""公开资源元数据只校验对象结构，位置语义由具体字段和 owner 声明。"""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Annotated

from pydantic import SerializerFunctionWrapHandler, WrapSerializer


def _is_file_url(value: str) -> bool:
    return value.lower().startswith(("file:/", "file:\\"))


def is_native_relative_path(value: object) -> bool:
    """运行态 cwd 只保存原生相对路径，不把 URI 或盘符相对路径当目录。"""
    return isinstance(value, str) and not (
        PureWindowsPath(value).anchor or _is_file_url(value) or value.startswith("boxteam://")
    )


def _serialize_resource_metadata(
    value: dict[str, object], handler: SerializerFunctionWrapHandler,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("公开资源元数据必须是对象")
    payload: dict[str, object] = handler(value)
    return payload


PublicResourceMetadata = Annotated[
    dict[str, object], WrapSerializer(_serialize_resource_metadata),
]
