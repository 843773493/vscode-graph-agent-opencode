"""fork 物化输入的严格解析边界。"""

from __future__ import annotations

import re
from collections.abc import Sequence

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


def optional_identity(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return required_text(value, field=field)

def required_sha256(value: object, *, field: str) -> str:
    digest = required_text(value, field=field)
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError(f"{field} 必须是小写 SHA-256 hex digest")
    return digest


__all__ = [
    "detail_copy_row",
    "non_negative_int",
    "one_of_text",
    "optional_identity",
    "optional_text",
    "required_sha256",
    "required_text",
    "sqlite_bool",
]
