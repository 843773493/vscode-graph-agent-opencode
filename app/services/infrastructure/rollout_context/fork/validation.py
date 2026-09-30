"""fork 物化输入的严格解析边界。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from app.domain.itemized.identity.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.assembly.validation import (
    non_negative_int,
    one_of_text,
    optional_text,
    required_text,
    sqlite_bool,
)


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
