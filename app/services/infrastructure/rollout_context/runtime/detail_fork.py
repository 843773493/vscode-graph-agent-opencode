"""显式 typed fork capability：认证源正文，只返回目标安全 artifact。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.domain.itemized.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    DetailUnavailableError,
    redacted_detail_payload,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    build_detail_payload,
)
from app.services.infrastructure.rollout_context.runtime.protected_detail import (
    ProtectedDetailBackend,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.runtime.detail_store import (
        ContextPlanDetailStore,
    )


@dataclass(frozen=True, slots=True)
class ForkDetailArtifact:
    record: DetailRecord = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    protected_bytes: bytes | None = field(repr=False)


class ForkDetailCapability:
    """只由 Saver 显式取得；不接受旧 record/locator，不发布文件。"""

    def __init__(
        self, store: ContextPlanDetailStore, backend: ProtectedDetailBackend | None
    ) -> None:
        self._store = store
        self._backend = backend

    def require_protected_key(self) -> None:
        if self._backend is None:
            raise DetailUnavailableError("detail-forbidden: protected fork 未注入 key")

    def prepare_detail(
        self,
        *,
        source_record: DetailRecord,
        target_ref: DetailRef,
        target_session_key: bytes,
    ) -> ForkDetailArtifact:
        if not isinstance(source_record, DetailRecord) or not isinstance(
            target_ref, DetailRef
        ):
            raise TypeError("fork detail 必须使用 typed record/ref")
        if source_record.session_id == target_ref.session_id:
            raise ValueError("fork detail source/target owner 必须不同")
        if (
            source_record.assembly_id == target_ref.assembly_id
            or source_record.detail_id == target_ref.detail_id
        ):
            raise ValueError(
                "fork detail 必须分配 target-local assembly/detail identity"
            )
        if type(target_session_key) is not bytes or len(target_session_key) != 32:
            raise ValueError("fork target session digest key 必须为 32 bytes")
        if source_record.sensitive:
            self.require_protected_key()
        # 普通 typed read 是唯一源认证链，涵盖 AES AAD、session HMAC、marker、长度。
        # 异常在 except 外重抛，避免底层解析异常携带敏感原文/cause/context。
        failed = False
        try:
            value = self._store.read(
                session_id=source_record.session_id,
                record=source_record,
                include_sensitive=source_record.sensitive,
            )
        except (RuntimeError, ValueError, TypeError, OSError):
            failed = True
        if failed:
            raise DetailUnavailableError(
                "detail-unavailable: fork source authentication/retention failed"
            )
        body = value["detail"]
        marker, digest = (body, None)
        if source_record.sensitive:
            marker, digest = redacted_detail_payload(
                body,
                session_key=target_session_key,
                detail_kind=source_record.detail_kind,
            )
            if digest == source_record.redacted_stable_digest:
                raise DetailUnavailableError(
                    "source-mismatch: fork 不得复用 source session digest key"
                )
        raw, record = build_detail_payload(
            detail_ref=target_ref,
            detail_kind=source_record.detail_kind,
            retention_class=source_record.retention_class,
            visibility=source_record.visibility,
            stored_detail=marker,
            source_revision=source_record.source_revision,
            length=source_record.length,
            detail_content_hash=None
            if source_record.sensitive
            else value["detail_content_hash"],
            redacted_stable_digest=digest,
            required=source_record.required,
            sensitive=source_record.sensitive,
            protection=source_record.protection,
            created_at=value["created_at"],
            expires_at=source_record.expires_at,
            checkpoint_ns=source_record.checkpoint_ns,
        )
        protected = None
        if source_record.sensitive:
            self.require_protected_key()
            if self._backend is None:
                raise DetailUnavailableError(
                    "detail-forbidden: protected fork 未注入 key"
                )
            encryption_failed = False
            try:
                protected = self._backend.encrypt(
                    record=record,
                    detail=body,
                    detail_content_hash=value["detail_content_hash"],
                )
            except (RuntimeError, ValueError, TypeError, OSError):
                encryption_failed = True
            if encryption_failed:
                raise DetailUnavailableError(
                    "detail-unavailable: fork target encryption failed"
                )
        return ForkDetailArtifact(record, raw, protected)
