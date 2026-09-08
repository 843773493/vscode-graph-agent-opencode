"""typed detail reference 与 SQLite key 的唯一严格编码边界。"""

from __future__ import annotations

import json

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes


def detail_ref_key(ref: DetailRef) -> str:
    if not isinstance(ref, DetailRef):
        raise TypeError("detail_ref 必须是 DetailRef，不接受裸 ID 或物理路径")
    return canonical_json_bytes(ref.to_dict()).decode("utf-8")


def optional_detail_ref_key(ref: DetailRef | None) -> str | None:
    return None if ref is None else detail_ref_key(ref)


def detail_ref_from_key(value: object) -> DetailRef:
    if not isinstance(value, str):
        raise TypeError("SQLite detail_ref 必须是规范 typed JSON key")
    ref = DetailRef.from_dict(json.loads(value))
    if detail_ref_key(ref) != value:
        raise ValueError("source-mismatch: SQLite detail_ref 不是规范 JCS key")
    return ref
