"""公开元数据的位置校验；正文和具名 Browser 页面 URL 不按位置字段解释。"""

from __future__ import annotations

from pathlib import PurePath, PureWindowsPath
from typing import Annotated

from pydantic import AfterValidator, SerializerFunctionWrapHandler, WrapSerializer

_LOCATION_FIELDS = frozenset({"root", "roots", "path", "paths", "location", "locations", "directory", "directories", "cwd"})
_CONFIG_POINTER_FIELDS = frozenset({"changed_paths", "applied_paths", "deferred_paths"})


def _is_location_field(field: str) -> bool:
    return field not in _CONFIG_POINTER_FIELDS and (
        field in _LOCATION_FIELDS or any(field.endswith(f"_{name}") for name in _LOCATION_FIELDS)
    )


def _is_file_url(value: str) -> bool:
    return value.lower().startswith(("file:/", "file:\\"))


def is_native_relative_path(value: object) -> bool:
    """运行态 cwd 只保存原生相对路径，不把 URI 或盘符相对路径当目录。"""
    return isinstance(value, str) and not (
        PureWindowsPath(value).anchor or _is_file_url(value) or value.startswith("boxteam://")
    )


def reject_metadata_real_paths(value: object, *, location: bool = False) -> None:
    """只检查结构化位置；不复制可能带原生位置的动态键或原值到公开错误。"""
    if isinstance(value, (str, PurePath)):
        path = str(value)
        if location and (PureWindowsPath(path).root or _is_file_url(path)):
            raise ValueError("公开元数据位置字段包含真实路径")
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "cwd_relative" and item is not None and not is_native_relative_path(item):
                raise ValueError("公开元数据工作目录不是原生相对路径")
            reject_metadata_real_paths(item, location=isinstance(key, str) and _is_location_field(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_metadata_real_paths(item, location=location)


def _validate_resource_metadata(value: dict[str, object]) -> dict[str, object]:
    reject_metadata_real_paths(value)
    return value


def _serialize_resource_metadata(
    value: dict[str, object], handler: SerializerFunctionWrapHandler,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("公开资源元数据必须是对象")
    payload: dict[str, object] = handler(value)
    reject_metadata_real_paths(payload)
    return payload


PublicResourceMetadata = Annotated[
    dict[str, object], AfterValidator(_validate_resource_metadata), WrapSerializer(_serialize_resource_metadata),
]
