"""fork 物化输入的严格解析边界。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from app.domain.itemized.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)


def required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"fork manifest {field} 必须是非空字符串")
    return value


def optional_text(value: object, *, field: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value):
        raise RuntimeError(f"fork manifest {field} 必须是非空字符串或 NULL")
    return value


def non_negative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"fork manifest {field} 必须是非负整数")
    return value


def sqlite_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, int) or isinstance(value, bool) or value not in {0, 1}:
        raise RuntimeError(f"fork manifest {field} 必须是 SQLite 0/1")
    return value == 1


def one_of_text(value: object, allowed: set[str], *, field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise RuntimeError(f"fork manifest {field} 值非法: {value!r}")
    return value


def json_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, str):
        raise TypeError(f"fork manifest {field} 必须是 JSON 文本")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"fork manifest {field} JSON 非法") from error
    if not isinstance(parsed, Mapping):
        raise TypeError(f"fork manifest {field} 必须是 object")
    return parsed


def detail_copy_row(
    row: Sequence[object],
) -> tuple[DetailRef, str, str, str, bool, str]:
    if len(row) != 7:
        raise RuntimeError("fork detail manifest 字段数量不一致")
    ref = detail_ref_from_key(row[0])
    if ref.assembly_id != row[1] or ref.detail_id != row[6]:
        raise RuntimeError("source-mismatch: fork detail key 与 identity 列不一致")
    return (
        ref,
        required_text(row[1], field="detail.assembly_id"),
        required_text(row[2], field="detail.relative_path"),
        required_text(row[3], field="detail.content_hash"),
        sqlite_bool(row[4], field="detail.required"),
        one_of_text(row[5], {"available", "unavailable"}, field="detail.status"),
    )


__all__ = [
    "detail_copy_row",
    "json_mapping",
    "non_negative_int",
    "one_of_text",
    "optional_text",
    "required_text",
    "sqlite_bool",
]
