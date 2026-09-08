"""显式 schema2→3 protected artifact 转换，不写盘也不返回敏感正文。

旧编码来自本任务实际源码读取记录（2026-09-07T19:49:03.790Z）：
rollout 01a07c06-50e8-73e0-9051-93b47e7529ae，
continuation 01a07c34-8cda-7d63-af12-1afc6f76e347，JSONL 第 13789 行。
完整旧 protected_detail.py 的 SHA-256：
62916f2b5898ae0ec51635887bef470ac59c8e9c96214da45d8cc76db6056955。
旧 blob 是 v1 magic + nonce(12) + AESGCM(ciphertext/tag)，旧 AAD 精确六字段。
冻结旧 writer 生成的 golden 位于独立 test_protected_detail_upgrade.py。
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.runtime.detail_keys import (
    ContextDetailKeyStore,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    DetailUnavailableError,
    expiry_time,
    redacted_detail_payload,
    session_detail_digest,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    build_detail_payload,
    require_protected_metadata,
)
from app.services.infrastructure.rollout_context.runtime.protected_detail import (
    ProtectedDetailBackend,
)

_AAD_FIELDS = frozenset(
    {
        "format_version",
        "session_id",
        "assembly_id",
        "detail_ref",
        "source_revision",
        "content_length",
    }
)
_BODY_FIELDS = _AAD_FIELDS | {"detail", "detail_content_hash", "redacted_stable_digest"}


@dataclass(frozen=True, slots=True)
class ProtectedDetailUpgrade:
    """准备好的 staging 内容；不持有解密正文，也不暴露旧 record 形状。"""

    record: DetailRecord = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    protected_bytes: bytes = field(repr=False)


def _source_aad(raw: bytes) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise TypeError("AAD 必须是 bytes")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != _AAD_FIELDS:
        raise ValueError("旧 AAD 字段不匹配")
    if canonical_json_bytes(value) != raw:
        raise ValueError("旧 AAD 不是 canonical JSON")
    if type(value["format_version"]) is not int or value["format_version"] != 1:
        raise ValueError("旧 AAD 版本不匹配")
    for name in ("session_id", "assembly_id", "detail_ref", "source_revision"):
        part = value[name]
        if not isinstance(part, str) or not part or "\x00" in part:
            raise ValueError("旧 AAD identity 非法")
    if type(value["content_length"]) is not int or value["content_length"] < 0:
        raise ValueError("旧 AAD length 非法")
    return value


def _authenticated_payload(
    old: Mapping[str, object],
    aad: Mapping[str, object],
) -> tuple[object, str]:
    if set(old) != _BODY_FIELDS or any(
        type(old[key]) is not type(value) or old[key] != value
        for key, value in aad.items()
    ):
        raise ValueError("认证 envelope 与原 AAD 不一致")
    body = old["detail"]
    body_hash = sha256_jcs(body)
    if (
        old["detail_content_hash"] != body_hash
        or len(canonical_json_bytes(body)) != aad["content_length"]
    ):
        raise ValueError("正文 hash/length 不一致")
    return body, body_hash


def _verified_digest(
    body: object,
    *,
    session_key: bytes,
    expected_digest: str,
    actual_digest: object,
) -> str:
    digest = session_detail_digest(body, session_key=session_key)
    if (
        not isinstance(expected_digest, str)
        or not expected_digest.isascii()
        or not isinstance(actual_digest, str)
        or not actual_digest.isascii()
        or not hmac.compare_digest(digest, expected_digest)
        or not hmac.compare_digest(digest, actual_digest)
    ):
        raise ValueError("session digest 不一致")
    return digest


def prepare_protected_detail_upgrade(
    *,
    backend: ProtectedDetailBackend | None,
    key_store: ContextDetailKeyStore,
    legacy_blob: bytes,
    legacy_aad: bytes,
    expected_digest: str,
    target_ref: DetailRef,
    detail_kind: str,
    retention_class: str,
    visibility: str,
    required: bool,
    created_at: str,
    expires_at: str | None,
    checkpoint_ns: str,
) -> ProtectedDetailUpgrade:
    """认证并校验旧正文后，仅准备新格式；迁移安装需要调用方另行提交。

    target_ref 显式指定重映射，session 必须保持一致；assembly/detail 可显式换 ID。
    expected_digest 必须来自 source registry/manifest，不能从密文自证。
    """
    stage = "key-required"
    try:
        if backend is None:
            raise ValueError("未注入 protected key")
        stage = "source-aad"
        aad = _source_aad(legacy_aad)
        stage = "source-owner"
        if not isinstance(target_ref, DetailRef):
            raise TypeError("target_ref 必须是 typed DetailRef")
        target_ref.require_owner(aad["session_id"])
        stage = "session-key-required"
        session_key = key_store.get(target_ref.session_id, create=False)
        stage = "authentication"
        old = backend.authenticate_schema2_blob(legacy_blob, aad=legacy_aad)
        stage = "source-manifest"
        body, body_hash = _authenticated_payload(old, aad)
        stage = "session-digest"
        digest = _verified_digest(
            body,
            session_key=session_key,
            expected_digest=expected_digest,
            actual_digest=old["redacted_stable_digest"],
        )
        marker, _digest = redacted_detail_payload(
            body, session_key=session_key, detail_kind=detail_kind
        )
        stage = "target-manifest"
        if expiry_time(created_at) is None:
            raise ValueError("created_at 必须显式提供")
        manifest, record = build_detail_payload(
            detail_ref=target_ref,
            detail_kind=detail_kind,
            retention_class=retention_class,
            visibility=visibility,
            stored_detail=marker,
            source_revision=aad["source_revision"],
            length=aad["content_length"],
            detail_content_hash=None,
            redacted_stable_digest=digest,
            required=required,
            sensitive=True,
            protection="protected",
            created_at=created_at,
            expires_at=expires_at,
            checkpoint_ns=checkpoint_ns,
        )
        stage = "target-encryption"
        encrypted = backend.encrypt(
            record=record, detail=body, detail_content_hash=body_hash
        )
        checked = backend.decrypt(encrypted, record=record)
        require_protected_metadata(checked, record)
        if (
            checked["detail"] != body
            or checked["detail_content_hash"] != body_hash
            or checked["redacted_stable_digest"] != digest
        ):
            raise ValueError("新密文 roundtrip 不一致")
        return ProtectedDetailUpgrade(record, manifest, encrypted)
    except Exception:  # noqa: BLE001 - 隐私边界统一换为不含正文的分阶段错误
        # decoder/JCS/IO/crypto 异常可能持有明文，不连接原异常，不格式化 payload。
        failure = f"schema-upgrade-protected-{stage}"
    # except 外抛出，连 __context__ 也不保留敏感 decoder exception。
    raise DetailUnavailableError(failure)


__all__ = ["ProtectedDetailUpgrade", "prepare_protected_detail_upgrade"]
