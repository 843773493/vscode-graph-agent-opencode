"""以 typed DetailRef 为唯一物理身份的 context detail store。"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_store import SessionCreationRecord
from app.domain.itemized.hashing import canonical_json_bytes_and_hash
from app.domain.itemized.identity.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.runtime.detail_files import DetailFiles
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    DetailUnavailableError,
    _manifest_bool,
    _manifest_non_negative_int,
    _manifest_string,
    detail_record_from_mapping,
    detail_relative_path,
    expiry_time,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    build_detail_payload,
    parse_detail_payload,
)
from app.services.infrastructure.rollout_context.runtime.detail_removal import (
    DetailRemoval,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)


@dataclass(frozen=True, slots=True)
class PreparedDetail:
    """只在本次 seal 内流转的冻结正文，不代表已发布的 durable detail。"""

    record: DetailRecord
    raw: bytes


class ContextPlanDetailStore:
    """正文只存储于 resolver 返回的当前 session 节点，不复用 source locator。"""

    def __init__(self, sessions_dir: str | Path) -> None:
        self.sessions_dir = Path(sessions_dir).resolve()
        self._resolver = get_session_path_resolver(self.sessions_dir)
        self._files = DetailFiles(self._resolver)
        self._removal = DetailRemoval(self._files)

    def prepare(
        self,
        *,
        session_id: str,
        thread_id: str,
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
    ) -> PreparedDetail:
        """冻结唯一精确正文与完整 manifest；sensitive 只控制访问，不改变持久化。"""
        ref = DetailRef(
            session_id, thread_id, assembly_id, "detail-" + secrets.token_hex(16)
        )
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
        effective_protection = protection or ("protected" if sensitive else "public")
        created_at = datetime.now(UTC)
        expires_at = (created_at + timedelta(days=retention_days)).isoformat()
        canonical_detail_bytes, original_hash = canonical_json_bytes_and_hash(detail)
        original_length = len(canonical_detail_bytes)
        # source_revision 的业务语义不随 typed identity 迁移改变。
        effective_revision = (
            original_hash if source_revision is None else source_revision
        )
        _manifest_string(effective_revision, field="source_revision")
        raw, record = build_detail_payload(
            detail_ref=ref,
            detail_kind=detail_kind,
            retention_class=retention_class,
            visibility=visibility,
            stored_detail=detail,
            source_revision=effective_revision,
            length=original_length,
            detail_content_hash=original_hash,
            required=required,
            sensitive=sensitive,
            protection=effective_protection,
            created_at=created_at.isoformat(),
            expires_at=expires_at,
            checkpoint_ns=checkpoint_ns,
        )
        return PreparedDetail(record, raw)

    def publish(
        self, prepared: PreparedDetail, *, creation_record: SessionCreationRecord | None = None,
    ) -> tuple[Path, ...]:
        """排他发布唯一正文；仅返回本次创建的文件，供 seal 回滚。"""
        if not isinstance(prepared, PreparedDetail):
            raise TypeError("detail publish 必须使用 PreparedDetail")
        ref = prepared.record.detail_ref
        _value, record = parse_detail_payload(prepared.raw, detail_ref=ref)
        if record != prepared.record:
            raise DetailUnavailableError("source-mismatch: prepared detail manifest 不一致")
        return (self._files.publish(ref, prepared.raw, creation_record=creation_record),)

    @staticmethod
    def discard_published(paths: Iterable[Path]) -> None:
        """调用方只可传入 publish 返回的本次排他创建路径。"""
        for path in reversed(tuple(paths)):
            path.unlink()

    def write(
        self,
        *,
        session_id: str,
        thread_id: str,
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
        values = {
            "session_id": session_id, "thread_id": thread_id, "assembly_id": assembly_id,
            "detail_kind": detail_kind, "retention_class": retention_class,
            "visibility": visibility, "detail": detail, "required": required,
            "sensitive": sensitive, "protection": protection, "retention_days": retention_days,
            "checkpoint_ns": checkpoint_ns, "source_revision": source_revision,
        }
        prepared = self.prepare(**values)
        self.publish(prepared)
        return prepared.record

    def read(
        self,
        *,
        session_id: str,
        thread_id: str,
        record: DetailRecord,
        include_sensitive: bool = False,
        snapshot: RolloutReadSnapshot | None = None,
    ) -> dict[str, object]:
        if snapshot is not None and snapshot.checkpoint_ns != record.checkpoint_ns:
            raise ValueError("read snapshot 与 detail namespace 不一致")
        return self._read_detail(
            record, include_sensitive=include_sensitive,
            read_raw=lambda: self._files.read(
                record.detail_ref, session_id=session_id, thread_id=thread_id,
                snapshot=snapshot,
            ),
        )

    def read_prepared(
        self, prepared: PreparedDetail, *, include_sensitive: bool = False
    ) -> dict[str, object]:
        """投影只读取显式准备值；校验算法与已提交 detail 完全相同。"""
        return self._read_detail(
            prepared.record, include_sensitive=include_sensitive, read_raw=lambda: prepared.raw,
        )

    def _read_detail(
        self, record: DetailRecord, *, include_sensitive: bool, read_raw: Callable[[], bytes],
    ) -> dict[str, object]:
        _manifest_bool(include_sensitive, field="include_sensitive")
        if record.status != "available" or record.availability != "available":
            raise DetailUnavailableError(f"assembly detail 不可用: {record.detail_ref}")
        expires = expiry_time(record.expires_at)
        if expires is not None and expires <= datetime.now(UTC):
            raise DetailUnavailableError(f"assembly detail 已过期: {record.detail_ref}")
        if record.sensitive and not include_sensitive:
            raise PermissionError(f"detail 受保护，未授权读取: {record.detail_ref}")
        value, manifest_record = parse_detail_payload(
            read_raw(), detail_ref=record.detail_ref,
        )
        if manifest_record != record:
            raise DetailUnavailableError(
                f"assembly detail manifest/hash 不匹配: {record.detail_ref}"
            )
        return value

    def remove(
        self, *, session_id: str, thread_id: str, record: DetailRecord
    ) -> None:
        """只负责正文清理；registry tombstone 必须由调用 owner 先提交。"""
        self._removal.remove(
            session_id=session_id, thread_id=thread_id, record=record
        )

    def gc(
        self,
        *,
        session_id: str,
        thread_id: str,
        expired_before: datetime,
        allowed_refs: Iterable[DetailRef],
    ) -> tuple[DetailRef, ...]:
        return self._removal.gc(
            session_id=session_id,
            thread_id=thread_id,
            expired_before=expired_before,
            allowed_refs=allowed_refs,
        )


__all__ = [
    "ContextPlanDetailStore",
    "DetailRecord",
    "DetailUnavailableError",
    "PreparedDetail",
    "detail_record_from_mapping",
    "detail_relative_path",
]
