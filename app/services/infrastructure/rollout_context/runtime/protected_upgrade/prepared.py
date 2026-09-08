"""仅认证迁移暂存的 typed artifact；不重新加密、发布或返回正文。"""

from __future__ import annotations

from dataclasses import replace

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.runtime.detail_keys import (
    ContextDetailKeyStore,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    DetailUnavailableError,
    expiry_time,
    redacted_detail_payload,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    parse_detail_payload,
    require_protected_metadata,
)
from app.services.infrastructure.rollout_context.runtime.protected_detail import (
    ProtectedDetailBackend,
)
from app.services.infrastructure.rollout_context.runtime.protected_upgrade import (
    _verified_digest,
)


def verify_prepared_detail_upgrade(
    *,
    backend: ProtectedDetailBackend | None,
    key_store: ContextDetailKeyStore,
    record: DetailRecord,
    manifest_bytes: bytes,
    protected_bytes: bytes,
) -> None:
    """绑定调用方已核定的 record，认证原 staged bytes 后允许 owner 原样复用。

    不读取 staged 路径或旧 artifact；路径安全、审计归属与安装仍由 migration
    owner 负责。过期时间被认证但不据当前时钟拒绝不可变的历史迁移材料。
    """
    stage = "key-required"
    try:
        if backend is None:
            raise ValueError("未注入 protected key")
        stage = "prepared-manifest"
        if not isinstance(record, DetailRecord):
            raise TypeError("record 必须是 typed DetailRecord")
        # 防止绕过 frozen dataclass 构造器后，以 bool/int 等宽松相等逃过比对。
        replace(record)
        if not isinstance(manifest_bytes, bytes) or not isinstance(
            protected_bytes, bytes
        ):
            raise TypeError("暂存 artifact 必须是 bytes")
        if record.protection != "protected" or record.availability != "available":
            raise ValueError("只认证 available protected detail")
        manifest, parsed = parse_detail_payload(
            manifest_bytes, detail_ref=record.detail_ref
        )
        if parsed != record or expiry_time(manifest["created_at"]) is None:
            raise ValueError("暂存 manifest 与预期 record 不一致")
        stage = "session-key-required"
        session_key = key_store.get(record.session_id, create=False)
        stage = "prepared-authentication"
        payload = backend.decrypt(protected_bytes, record=record)
        require_protected_metadata(payload, record)
        stage = "prepared-integrity"
        body = payload["detail"]
        if (
            payload["detail_content_hash"] != sha256_jcs(body)
            or len(canonical_json_bytes(body)) != record.length
        ):
            raise ValueError("暂存正文 hash/length 不一致")
        _verified_digest(
            body,
            session_key=session_key,
            expected_digest=record.redacted_stable_digest,
            actual_digest=payload["redacted_stable_digest"],
        )
        marker, _digest = redacted_detail_payload(
            body, session_key=session_key, detail_kind=record.detail_kind
        )
        if canonical_json_bytes(manifest["detail"]) != canonical_json_bytes(marker):
            raise ValueError("暂存 marker 的类型、分类或长度与认证正文不一致")
    except Exception:  # noqa: BLE001 - decoder/crypto/IO 原异常可能包含敏感正文
        failure = f"schema-upgrade-protected-{stage}"
    else:
        return
    # 在 except 外抛出，连 __context__ 也不携带可能包含正文的原始异常。
    raise DetailUnavailableError(failure)


__all__ = ["verify_prepared_detail_upgrade"]
