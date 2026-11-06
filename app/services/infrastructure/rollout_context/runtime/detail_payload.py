"""typed detail v3 文件 envelope 的解析与完整性校验。"""

from __future__ import annotations

import json
from hashlib import sha256

from app.domain.itemized.hashing import (
    canonical_json_bytes,
    canonical_json_bytes_and_hash,
)
from app.domain.itemized.identity.detail_ref import DetailRef
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
    stored_detail_bytes: bytes,
    source_revision: str,
    length: int,
    detail_content_hash: str,
    required: bool,
    sensitive: bool,
    protection: str,
    created_at: str,
    expires_at: str | None,
    checkpoint_ns: str,
) -> tuple[bytes, DetailRecord]:
    """唯一 exact 正文 envelope owner；不读取旧开发格式。"""
    value = {
        "format_version": 3,
        "detail_ref": detail_ref.to_dict(),
        "detail_kind": detail_kind,
        "retention_class": retention_class,
        "visibility": visibility,
        "source_revision": source_revision,
        "length": length,
        "detail_content_hash": detail_content_hash,
        "required": required,
        "sensitive": sensitive,
        "protection": protection,
        "created_at": created_at,
        "expires_at": expires_at,
        "checkpoint_ns": checkpoint_ns,
    }
    # v3 固定字段按 JCS 排序，detail 紧邻 detail_content_hash 之前。
    # 正文已在 prepare 冻结，用同一份字节组装 envelope，不再遍历和编码原始容器。
    prefix, suffix = canonical_json_bytes(value).split(b',"detail_content_hash":', 1)
    raw = b"".join((prefix, b',"detail":', stored_detail_bytes, b',"detail_content_hash":', suffix))
    envelope_hash = "sha256:jcs:v1:" + sha256(raw).hexdigest()
    return raw, _record_from_payload(value, detail_ref, envelope_hash)


def parse_detail_payload(
    raw: bytes, *, detail_ref: DetailRef
) -> tuple[dict[str, object], DetailRecord]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DetailUnavailableError("assembly detail 无法读取：非法 JSON") from error
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise DetailUnavailableError(
            "assembly detail manifest 必须有且仅有 typed v3 字段"
        )
    if type(value["format_version"]) is not int or value["format_version"] != 3:
        raise DetailUnavailableError("assembly detail manifest format_version 非法")
    try:
        canonical, envelope_hash = canonical_json_bytes_and_hash(value)
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
    record = _record_from_payload(value, ref, envelope_hash)
    # envelope 已逐字节核验为 JCS，身份与 metadata 也已校验；正文复用这次编码。
    # 前置字符串中的引号已转义；正文可含同名键，故取最后一个外层 hash 分隔符。
    start = canonical.index(b',"detail":') + len(b',"detail":')
    end = canonical.rindex(b',"detail_content_hash":')
    canonical_detail_bytes = canonical[start:end]
    detail_hash = "sha256:jcs:v1:" + sha256(canonical_detail_bytes).hexdigest()
    if (
        value["detail_content_hash"] != detail_hash
        or len(canonical_detail_bytes) != record.length
    ):
        raise DetailUnavailableError("assembly detail content manifest hash/length 不一致")
    return value, record


def _record_from_payload(value, ref: DetailRef, envelope_hash: str) -> DetailRecord:
    """编码与读取共用typed manifest构造；字段约束由DetailRecord持有。"""
    return DetailRecord(
        session_id=ref.session_id,
        thread_id=ref.thread_id,
        assembly_id=ref.assembly_id,
        detail_id=ref.detail_id,
        detail_kind=value["detail_kind"],
        retention_class=value["retention_class"],
        visibility=value["visibility"],
        relative_path=detail_relative_path(ref).as_posix(),
        content_hash=envelope_hash,
        length=value["length"],
        source_revision=value["source_revision"],
        required=value["required"],
        sensitive=value["sensitive"],
        status="available",
        expires_at=value["expires_at"],
        checkpoint_ns=value["checkpoint_ns"],
        protection=value["protection"],
        availability="available",
    )

__all__ = ["build_detail_payload", "parse_detail_payload"]
