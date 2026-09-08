"""以 typed DetailRef 为唯一物理身份的 context detail store。"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.runtime.detail_files import DetailFiles
from app.services.infrastructure.rollout_context.runtime.detail_keys import (
    ContextDetailKeyStore,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    DetailUnavailableError,
    _manifest_bool,
    _manifest_non_negative_int,
    _manifest_string,
    detail_record_from_mapping,
    detail_relative_path,
    expiry_time,
    protected_detail_relative_path,
    redacted_detail_payload,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    build_detail_payload,
    parse_detail_payload,
    require_protected_metadata,
)
from app.services.infrastructure.rollout_context.runtime.detail_removal import (
    DetailRemoval,
)
from app.services.infrastructure.rollout_context.runtime.protected_detail import (
    ProtectedDetailBackend,
    ProtectedDetailError,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.migration.schema_v3.model import (
        SchemaV3DetailCapability,
    )
    from app.services.infrastructure.rollout_context.runtime.detail_fork import (
        ForkDetailCapability,
    )
    from app.services.infrastructure.rollout_context.runtime.protected_upgrade import (
        ProtectedDetailUpgrade,
    )


class ContextPlanDetailStore:
    """正文只存储于 resolver 返回的当前 session 节点，不复用 source locator。"""

    def __init__(
        self, sessions_dir: str | Path, *, protected_key: bytes | None = None
    ) -> None:
        self.sessions_dir = Path(sessions_dir).resolve()
        self._resolver = get_session_path_resolver(self.sessions_dir)
        self._files = DetailFiles(self._resolver)
        self._removal = DetailRemoval(self._files)
        self._detail_key_store = ContextDetailKeyStore(self._resolver)
        self._protected_backend = (
            ProtectedDetailBackend(protected_key) if protected_key is not None else None
        )

    @property
    def supports_protected_details(self) -> bool:
        return self._protected_backend is not None

    def fork_detail_capability(self) -> ForkDetailCapability:
        """显式跨 owner 复制能力；不把 cipher、明文或原 session key 交给 fork。"""
        from app.services.infrastructure.rollout_context.runtime.detail_fork import (
            ForkDetailCapability,
        )

        return ForkDetailCapability(self, self._protected_backend)

    def schema_v3_detail_capability(self) -> SchemaV3DetailCapability:
        """仅由 Saver 的显式 upgrade 注入 storage；不加载 migration parser。"""
        from app.services.infrastructure.rollout_context.runtime.protected_upgrade.capability import (
            RuntimeSchemaV3DetailCapability,
        )

        return RuntimeSchemaV3DetailCapability(
            backend=self._protected_backend,
            key_store=self._detail_key_store,
        )

    def prepare_protected_detail_upgrade(
        self,
        *,
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
        """仅供显式 artifact migration；不读取旧 locator，不发布任何文件。"""
        from app.services.infrastructure.rollout_context.runtime.protected_upgrade import (
            prepare_protected_detail_upgrade,
        )

        return prepare_protected_detail_upgrade(
            backend=self._protected_backend,
            key_store=self._detail_key_store,
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

    def write(
        self,
        *,
        session_id: str,
        assembly_id: str,
        detail_kind: str,
        retention_class: str,
        visibility: str,
        detail: object,
        required: bool = False,
        sensitive: bool = False,
        protection: str | None = None,
        retention_days: int = 30,
        checkpoint_ns: str = "",
        source_revision: str | None = None,
    ) -> DetailRecord:
        ref = DetailRef(session_id, assembly_id, "detail-" + secrets.token_hex(16))
        _manifest_bool(required, field="required")
        _manifest_bool(sensitive, field="sensitive")
        _manifest_non_negative_int(retention_days, field="retention_days")
        _manifest_string(detail_kind, field="detail_kind")
        _manifest_string(retention_class, field="retention_class")
        _manifest_string(visibility, field="visibility")
        if visibility not in {"public", "internal", "private"}:
            raise DetailUnavailableError("detail manifest visibility 非法")
        if not isinstance(checkpoint_ns, str):
            raise DetailUnavailableError("detail manifest checkpoint_ns 非法")
        if protection is not None and (
            not isinstance(protection, str)
            or protection not in {"public", "redacted", "protected"}
        ):
            raise ValueError("未知 detail protection")
        if not sensitive and protection not in {None, "public"}:
            raise ValueError("非敏感 detail 不能声明 redacted/protected protection")
        if sensitive and protection == "public":
            raise ValueError("敏感 detail 不能声明 public protection")
        effective_protection = (
            protection
            if protection is not None
            else (
                ("protected" if self.supports_protected_details else "redacted")
                if sensitive
                else "public"
            )
        )
        if effective_protection == "protected" and self._protected_backend is None:
            raise DetailUnavailableError(
                "protected assembly detail 需要注入 protected detail backend"
            )
        created_at = datetime.now(UTC)
        expires_at = (created_at + timedelta(days=retention_days)).isoformat()
        original_hash = sha256_jcs(detail)
        original_length = len(canonical_json_bytes(detail))
        # source_revision 的业务语义不随 typed identity 迁移改变。
        effective_revision = (
            original_hash if source_revision is None else source_revision
        )
        _manifest_string(effective_revision, field="source_revision")
        redaction_digest = None
        stored_detail = detail
        if sensitive:
            stored_detail, redaction_digest = redacted_detail_payload(
                detail,
                session_key=self._detail_key_store.get(session_id, create=True),
                detail_kind=detail_kind,
            )
        raw, record = build_detail_payload(
            detail_ref=ref,
            detail_kind=detail_kind,
            retention_class=retention_class,
            visibility=visibility,
            stored_detail=stored_detail,
            source_revision=effective_revision,
            length=original_length,
            detail_content_hash=None if sensitive else original_hash,
            redacted_stable_digest=redaction_digest,
            required=required,
            sensitive=sensitive,
            protection=effective_protection,
            created_at=created_at.isoformat(),
            expires_at=expires_at,
            checkpoint_ns=checkpoint_ns,
        )
        protected_raw = None
        if effective_protection == "protected":
            if self._protected_backend is None:
                raise DetailUnavailableError("protected detail backend 不可用")
            protected_raw = self._protected_backend.encrypt(
                record=record,
                detail=detail,
                detail_content_hash=original_hash,
            )
        published: list[bool] = []
        try:
            if protected_raw is not None:
                self._files.publish(ref, protected_raw, protected=True)
                published.append(True)
            self._files.publish(ref, raw)
            published.append(False)
        except BaseException:
            # 只回滚本次排他创建的叶文件，绝不删除碰撞身份的既有内容。
            for protected in reversed(published):
                path = self._files.path(ref, protected=protected)
                if path is not None:
                    path.unlink()
            raise
        return record

    def read(
        self,
        *,
        session_id: str,
        record: DetailRecord,
        include_sensitive: bool = False,
    ) -> dict[str, object]:
        record.detail_ref.require_owner(session_id)
        _manifest_bool(include_sensitive, field="include_sensitive")
        if record.status != "available" or record.availability != "available":
            raise DetailUnavailableError(f"assembly detail 不可用: {record.detail_ref}")
        expires = expiry_time(record.expires_at)
        if expires is not None and expires <= datetime.now(UTC):
            raise DetailUnavailableError(f"assembly detail 已过期: {record.detail_ref}")
        if record.sensitive and not include_sensitive:
            raise PermissionError(f"detail 受保护，未授权读取: {record.detail_ref}")
        if record.sensitive and record.protection != "protected":
            raise DetailUnavailableError(
                "sensitive assembly detail 只有 redacted marker，当前没有 protected body"
            )
        value, manifest_record = parse_detail_payload(
            self._files.read(record.detail_ref),
            detail_ref=record.detail_ref,
        )
        if manifest_record != record:
            raise DetailUnavailableError(
                f"assembly detail manifest/hash 不匹配: {record.detail_ref}"
            )
        if record.protection != "protected":
            return value
        if self._protected_backend is None:
            raise DetailUnavailableError("protected assembly detail backend 不可用")
        try:
            protected_value = self._protected_backend.decrypt(
                self._files.read(record.detail_ref, protected=True),
                record=record,
            )
        except ProtectedDetailError as error:
            raise DetailUnavailableError(
                f"protected assembly detail 校验失败: {record.detail_ref}"
            ) from error
        require_protected_metadata(protected_value, record)
        body = protected_value["detail"]
        body_hash = sha256_jcs(body)
        if (
            protected_value["detail_content_hash"] != body_hash
            or protected_value["redacted_stable_digest"]
            != record.redacted_stable_digest
        ):
            raise DetailUnavailableError("protected assembly detail manifest 不匹配")
        marker, expected_digest = redacted_detail_payload(
            body,
            session_key=self._detail_key_store.get(session_id, create=False),
            detail_kind=record.detail_kind,
        )
        if expected_digest != record.redacted_stable_digest:
            raise DetailUnavailableError("protected assembly detail provenance 不匹配")
        if len(canonical_json_bytes(body)) != record.length:
            raise DetailUnavailableError("protected assembly detail length 不匹配")
        if marker != value["detail"]:
            raise DetailUnavailableError(
                "protected assembly detail marker 类型/分类不匹配"
            )
        return {**value, "detail": body, "detail_content_hash": body_hash}

    def remove(self, *, session_id: str, record: DetailRecord) -> None:
        """只负责正文清理；registry tombstone 必须由调用 owner 先提交。"""
        self._removal.remove(session_id=session_id, record=record)

    def gc(
        self,
        *,
        session_id: str,
        expired_before: datetime,
        allowed_refs: Iterable[DetailRef],
    ) -> tuple[DetailRef, ...]:
        return self._removal.gc(
            session_id=session_id,
            expired_before=expired_before,
            allowed_refs=allowed_refs,
        )


__all__ = [
    "ContextPlanDetailStore",
    "DetailRecord",
    "DetailUnavailableError",
    "detail_record_from_mapping",
    "detail_relative_path",
    "protected_detail_relative_path",
    "redacted_detail_payload",
]
