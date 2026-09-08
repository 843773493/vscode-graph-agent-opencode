"""typed detail v2 文件 envelope 的解析与完整性校验。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    DetailUnavailableError,
    detail_relative_path,
)

_FIELDS = frozenset(
    {
        "format_version",
        "detail_ref",
        "detail_kind",
        "retention_class",
        "visibility",
        "length",
        "source_revision",
        "required",
        "sensitive",
        "protection",
        "expires_at",
        "checkpoint_ns",
        "detail_content_hash",
        "redacted_stable_digest",
        "protected_body",
        "detail",
        "created_at",
    }
)


def build_detail_payload(
    *,
    detail_ref: DetailRef,
    detail_kind: str,
    retention_class: str,
    visibility: str,
    stored_detail: object,
    source_revision: str,
    length: int,
    detail_content_hash: str | None,
    redacted_stable_digest: str | None,
    required: bool,
    sensitive: bool,
    protection: str,
    created_at: str,
    expires_at: str | None,
    checkpoint_ns: str,
) -> tuple[bytes, DetailRecord]:
    """普通 write 与显式升级共用唯一的新格式 envelope owner。"""
    raw = canonical_json_bytes(
        {
            "format_version": 2,
            "detail_ref": detail_ref.to_dict(),
            "detail_kind": detail_kind,
            "retention_class": retention_class,
            "visibility": visibility,
            "detail": stored_detail,
            "source_revision": source_revision,
            "length": length,
            "detail_content_hash": detail_content_hash,
            "redacted_stable_digest": redacted_stable_digest,
            "required": required,
            "sensitive": sensitive,
            "protection": protection,
            "protected_body": protection == "protected",
            "created_at": created_at,
            "expires_at": expires_at,
            "checkpoint_ns": checkpoint_ns,
        }
    )
    _payload, record = parse_detail_payload(raw, detail_ref=detail_ref)
    return raw, record


def parse_detail_payload(
    raw: bytes, *, detail_ref: DetailRef
) -> tuple[dict[str, object], DetailRecord]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DetailUnavailableError("assembly detail 无法读取：非法 JSON") from error
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise DetailUnavailableError(
            "assembly detail manifest 必须有且仅有 typed v2 字段"
        )
    if type(value["format_version"]) is not int or value["format_version"] != 2:
        raise DetailUnavailableError("assembly detail manifest format_version 非法")
    try:
        canonical = canonical_json_bytes(value)
        ref = DetailRef.from_dict(value["detail_ref"])
    except (TypeError, ValueError) as error:
        raise DetailUnavailableError(
            "assembly detail manifest typed JSON 非法"
        ) from error
    if canonical != raw:
        raise DetailUnavailableError("assembly detail manifest 不是 canonical JSON")
    if ref != detail_ref:
        raise DetailUnavailableError(
            "source-mismatch: detail 文件与 typed identity 不一致"
        )
    if not isinstance(value["created_at"], str) or not value["created_at"]:
        raise DetailUnavailableError("assembly detail created_at 非法")
    record = DetailRecord(
        session_id=ref.session_id,
        assembly_id=ref.assembly_id,
        detail_id=ref.detail_id,
        detail_kind=value["detail_kind"],
        retention_class=value["retention_class"],
        visibility=value["visibility"],
        relative_path=detail_relative_path(ref).as_posix(),
        content_hash=sha256_jcs(value),
        length=value["length"],
        source_revision=value["source_revision"],
        required=value["required"],
        sensitive=value["sensitive"],
        status="available",
        expires_at=value["expires_at"],
        checkpoint_ns=value["checkpoint_ns"],
        redacted_stable_digest=value["redacted_stable_digest"],
        protection=value["protection"],
        availability="available",
    )
    if type(value["protected_body"]) is not bool or value["protected_body"] != (
        record.protection == "protected"
    ):
        raise DetailUnavailableError("assembly detail protection manifest 不一致")
    if record.sensitive:
        marker = value["detail"]
        if (
            not isinstance(marker, dict)
            or not isinstance(marker.get("value_type"), str)
            or marker["value_type"]
            not in {"null", "boolean", "string", "number", "object", "array"}
            or type(marker.get("redacted")) is not bool
            or type(marker.get("length")) is not int
            or marker
            != {
                "redacted": True,
                "redacted_stable_digest": record.redacted_stable_digest,
                "value_type": marker["value_type"],
                "detail_kind": record.detail_kind,
                "length": record.length,
            }
            or value["detail_content_hash"] is not None
        ):
            raise DetailUnavailableError(
                "sensitive assembly detail redaction manifest 不一致"
            )
    elif (
        value["detail_content_hash"] != sha256_jcs(value["detail"])
        or len(canonical_json_bytes(value["detail"])) != record.length
    ):
        raise DetailUnavailableError(
            "assembly detail content manifest hash/length 不一致"
        )
    return value, record


def protected_aad(record: DetailRecord) -> dict[str, object]:
    """所有安全和 retention 字段均绑定到同一个 typed owner，不能重新组合第二身份。"""
    if not isinstance(record, DetailRecord):
        raise TypeError("protected detail 必须使用 DetailRecord")
    return {
        "format_version": 2,
        "detail_ref": record.detail_ref.to_dict(),
        "source_revision": record.source_revision,
        "length": record.length,
        "detail_kind": record.detail_kind,
        "retention_class": record.retention_class,
        "visibility": record.visibility,
        "expires_at": record.expires_at,
        "protection": record.protection,
        "checkpoint_ns": record.checkpoint_ns,
        "required": record.required,
        "sensitive": record.sensitive,
        "content_hash": record.content_hash,
    }


def require_protected_metadata(
    value: Mapping[str, object], record: DetailRecord
) -> None:
    if set(value) != set(protected_aad(record)) | {
        "detail",
        "detail_content_hash",
        "redacted_stable_digest",
    }:
        raise DetailUnavailableError("protected assembly detail manifest 字段不匹配")
    for key, expected in protected_aad(record).items():
        if value.get(key) != expected or type(value.get(key)) is not type(expected):
            raise DetailUnavailableError("protected assembly detail manifest 不匹配")


__all__ = [
    "build_detail_payload",
    "parse_detail_payload",
    "protected_aad",
    "require_protected_metadata",
]
