"""Saver 显式 schema3 升级注入的 capability；不导入 migration parser。"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.runtime.detail_keys import (
    ContextDetailKeyStore,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    DetailUnavailableError,
)
from app.services.infrastructure.rollout_context.runtime.protected_detail import (
    ProtectedDetailBackend,
)
from app.services.infrastructure.rollout_context.runtime.protected_upgrade import (
    ProtectedDetailUpgrade,
    prepare_protected_detail_upgrade,
)
from app.services.infrastructure.rollout_context.runtime.protected_upgrade.prepared import (
    verify_prepared_detail_upgrade,
)


def _legacy_aad_bytes(
    envelope: Mapping[str, object],
    session_id: str,
    detail_id: str,
) -> bytes:
    return canonical_json_bytes(
        {
            "format_version": envelope["format_version"],
            "session_id": session_id,
            "assembly_id": envelope["assembly_id"],
            "detail_ref": detail_id,
            "source_revision": envelope["source_revision"],
            "content_length": envelope["content_length"],
        }
    )


class RuntimeSchemaV3DetailCapability:
    """实现 SchemaV3DetailCapability 结构化协议，密钥由 runtime owner 注入。"""

    def __init__(
        self,
        *,
        backend: ProtectedDetailBackend | None,
        key_store: ContextDetailKeyStore,
    ) -> None:
        self._backend = backend
        self._key_store = key_store

    def require_protected_key(self) -> None:
        if self._backend is None:
            raise DetailUnavailableError("schema-upgrade-protected-key-required")

    def verify_prepared_detail(
        self,
        *,
        record: DetailRecord,
        manifest_bytes: bytes,
        protected_bytes: bytes,
    ) -> None:
        """只认证 staged typed artifact；成功后 migration 原样复用随机密文。"""
        verify_prepared_detail_upgrade(
            backend=self._backend,
            key_store=self._key_store,
            record=record,
            manifest_bytes=manifest_bytes,
            protected_bytes=protected_bytes,
        )

    def prepare_legacy_detail(
        self,
        *,
        legacy_envelope: Mapping[str, object],
        legacy_blob: bytes,
        legacy_session_id: str,
        legacy_detail_id: str,
        target_ref: DetailRef,
        detail_kind: str,
        retention_class: str,
        visibility: str,
        required: bool,
        checkpoint_ns: str,
        expected_digest: str,
    ) -> ProtectedDetailUpgrade:
        """旧 marker 的类型只能由已认证正文确定；同时返回新的三份绑定产物。"""
        self.require_protected_key()
        try:
            if not isinstance(legacy_envelope, Mapping):
                raise TypeError("旧 envelope 必须是 mapping")
            if (
                legacy_envelope["sensitive"] is not True
                or legacy_envelope["protected_body"] is not True
                or legacy_envelope["protection"] != "protected"
                or legacy_envelope["detail_content_hash"] is not None
                or legacy_envelope["redacted_stable_digest"] != expected_digest
                or legacy_envelope["detail"]["redacted"] is not True
                or legacy_envelope["detail"]
                != {
                    "redacted": True,
                    "redacted_stable_digest": expected_digest,
                }
            ):
                raise ValueError("旧 protected manifest 不一致")
            legacy_aad = _legacy_aad_bytes(
                legacy_envelope, legacy_session_id, legacy_detail_id
            )
            created_at = legacy_envelope["created_at"]
            expires_at = legacy_envelope["gc_after"]
        except Exception:  # noqa: BLE001 - 不把旧 envelope 或 decoder 异常带出隐私边界
            failure = "schema-upgrade-protected-source-envelope"
        else:
            return prepare_protected_detail_upgrade(
                backend=self._backend,
                key_store=self._key_store,
                legacy_blob=legacy_blob,
                legacy_aad=legacy_aad,
                expected_digest=expected_digest,
                target_ref=target_ref,
                detail_kind=detail_kind,
                retention_class=retention_class,
                visibility=visibility,
                required=required,
                created_at=created_at,
                expires_at=expires_at,
                checkpoint_ns=checkpoint_ns,
            )
        raise DetailUnavailableError(failure)


__all__ = ["RuntimeSchemaV3DetailCapability"]
