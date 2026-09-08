"""Context detail 的 typed identity、物理 locator 与严格 durable manifest。"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes


class DetailUnavailableError(RuntimeError):
    """请求需要的 assembly 详情不可用。"""


def detail_relative_path(detail_ref: DetailRef) -> Path:
    """唯一普通 locator：不添加扩展名，不接受字符串或调用方路径。"""
    if not isinstance(detail_ref, DetailRef):
        raise TypeError("detail_ref 必须是 DetailRef")
    return Path(
        "rollout", "context-plan-details", detail_ref.assembly_id, detail_ref.detail_id
    )


def protected_detail_relative_path(detail_ref: DetailRef) -> Path:
    detail_relative_path(detail_ref)
    return Path(
        "rollout",
        "context-plan-details-protected",
        detail_ref.assembly_id,
        f"{detail_ref.detail_id}.bin",
    )


def redacted_detail_payload(
    detail: object, *, session_key: bytes, detail_kind: str
) -> tuple[dict[str, object], str]:
    """保持既有 session v1 摘要算法；不能因 locator 迁移改变摘要含义。"""
    _manifest_string(detail_kind, field="detail_kind")
    raw = canonical_json_bytes(detail)
    digest = session_detail_digest(detail, session_key=session_key)
    return {
        "redacted": True,
        "redacted_stable_digest": digest,
        "value_type": detail_value_type(detail),
        "detail_kind": detail_kind,
        "length": len(raw),
    }, digest


def session_detail_digest(detail: object, *, session_key: bytes) -> str:
    """统一既有 session v1 摘要；迁移验真不必伪造 marker 的业务分类。"""
    return (
        "hmac-sha256:session:v1:"
        + hmac.new(
            session_key,
            canonical_json_bytes(detail),
            hashlib.sha256,
        ).hexdigest()
    )


def detail_value_type(detail: object) -> str:
    """仅描述 JSON 类型；敏感业务分类使用调用方显式传入的 detail_kind。"""
    if detail is None:
        return "null"
    if isinstance(detail, bool):
        return "boolean"
    if isinstance(detail, str):
        return "string"
    if isinstance(detail, (int, float)):
        return "number"
    if isinstance(detail, Mapping):
        return "object"
    if isinstance(detail, (list, tuple)):
        return "array"
    raise TypeError("detail 必须是 JSON value")


def _manifest_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DetailUnavailableError(f"assembly detail manifest {field} 非法")
    return value


def _manifest_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise DetailUnavailableError(f"assembly detail manifest {field} 必须是 boolean")
    return value


def _manifest_non_negative_int(value: object, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise DetailUnavailableError(f"assembly detail manifest {field} 必须是非负整数")
    return value


def expiry_time(value: object) -> datetime | None:
    if value is None:
        return None
    text = _manifest_string(value, field="expires_at")
    try:
        expiry = datetime.fromisoformat(text)
    except ValueError as error:
        raise DetailUnavailableError("assembly detail expires_at 非法") from error
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise DetailUnavailableError("assembly detail expires_at 缺少时区")
    return expiry


@dataclass(frozen=True, slots=True)
class DetailRecord:
    session_id: str
    assembly_id: str
    detail_id: str
    detail_kind: str
    retention_class: str
    visibility: str
    relative_path: str
    content_hash: str
    length: int
    source_revision: str
    required: bool
    sensitive: bool
    status: str
    expires_at: str | None = None
    checkpoint_ns: str = ""
    redacted_stable_digest: str | None = None
    protection: str = "public"
    availability: str = "available"

    @property
    def detail_ref(self) -> DetailRef:
        return DetailRef(self.session_id, self.assembly_id, self.detail_id)

    def __post_init__(self) -> None:
        ref = self.detail_ref
        for name in (
            "detail_kind",
            "retention_class",
            "content_hash",
            "source_revision",
            "visibility",
            "protection",
            "status",
            "availability",
        ):
            _manifest_string(getattr(self, name), field=name)
        if self.relative_path != detail_relative_path(ref).as_posix():
            raise DetailUnavailableError(
                "detail relative_path 与 typed identity 不一致"
            )
        _manifest_non_negative_int(self.length, field="length")
        _manifest_bool(self.required, field="required")
        _manifest_bool(self.sensitive, field="sensitive")
        if not isinstance(self.checkpoint_ns, str):
            raise DetailUnavailableError("detail manifest checkpoint_ns 非法")
        if self.visibility not in {"public", "internal", "private"}:
            raise DetailUnavailableError("detail manifest visibility 非法")
        if self.protection not in {"public", "redacted", "protected"}:
            raise DetailUnavailableError("detail manifest protection 非法")
        if (
            self.status not in {"available", "unavailable"}
            or self.availability != self.status
        ):
            raise DetailUnavailableError("detail manifest status/availability 不一致")
        if self.sensitive != (self.protection != "public"):
            raise DetailUnavailableError(
                "detail manifest sensitive 与 protection 不一致"
            )
        if self.sensitive:
            _manifest_string(
                self.redacted_stable_digest, field="redacted_stable_digest"
            )
        elif self.redacted_stable_digest is not None:
            raise DetailUnavailableError(
                "非敏感 detail 不得携带 redacted_stable_digest"
            )
        expiry_time(self.expires_at)


def detail_record_from_mapping(raw: Mapping[str, object]) -> DetailRecord:
    """消费 registry 已解码的 typed ref；SQLite key 只由 assembly.detail_identity 编解码。"""
    if not isinstance(raw, Mapping):
        raise DetailUnavailableError("detail manifest 必须是 mapping")
    if {"gc_after", "content_length"} & set(raw):
        raise DetailUnavailableError(
            "detail manifest 禁止旧 gc_after/content_length 字段"
        )
    ref = raw.get("detail_ref")
    if not isinstance(ref, DetailRef):
        raise DetailUnavailableError(
            "detail manifest detail_ref 必须是 typed DetailRef"
        )
    required_fields = {
        "session_id",
        "assembly_id",
        "detail_id",
        "detail_kind",
        "retention_class",
        "visibility",
        "relative_path",
        "content_hash",
        "length",
        "source_revision",
        "required",
        "sensitive",
        "status",
        "expires_at",
        "checkpoint_ns",
        "redacted_stable_digest",
        "protection",
        "availability",
    }
    missing = required_fields - set(raw)
    if missing:
        raise DetailUnavailableError(
            "detail manifest 缺少字段: " + ",".join(sorted(missing))
        )
    values = {name: raw[name] for name in required_fields}
    for name in ("required", "sensitive"):
        # SQLite 仅允许真正的整数 0/1；不能宽松接受字符串、float 或 bool。
        value = values[name]
        if type(value) is not int or value not in (0, 1):
            raise DetailUnavailableError(f"detail manifest {name} 必须是 SQLite 0/1")
        values[name] = value == 1
    record = DetailRecord(**values)
    if ref != record.detail_ref:
        raise DetailUnavailableError(
            "source-mismatch: detail manifest typed identity 不一致"
        )
    return record


__all__ = [
    "DetailRecord",
    "DetailUnavailableError",
    "detail_record_from_mapping",
    "detail_relative_path",
    "protected_detail_relative_path",
    "redacted_detail_payload",
]
