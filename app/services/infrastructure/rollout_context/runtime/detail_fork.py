"""显式 typed fork capability：认证源正文，只返回目标安全 artifact。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.identity.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    DetailUnavailableError,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    build_detail_payload,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.runtime.detail_store import (
        ContextPlanDetailStore,
    )


@dataclass(frozen=True, slots=True)
class ForkDetailArtifact:
    record: DetailRecord = field(repr=False)
    manifest_bytes: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ForkDetailSourceSnapshot:
    """已在 source guard 内认证并冻结的 detail 正文。"""

    source_record: DetailRecord = field(repr=False)
    value_bytes: bytes | None = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source_record, DetailRecord):
            raise TypeError("fork detail source snapshot 必须绑定 DetailRecord")
        if self.value_bytes is not None:
            if type(self.value_bytes) is not bytes:
                raise TypeError("fork detail snapshot 正文必须是 bytes 或 None")
            value = json.loads(self.value_bytes)
            if not isinstance(value, dict):
                raise ValueError("fork detail snapshot value 必须是 JSON object")


class ForkDetailCapability:
    """只由 Saver 显式取得；不接受旧 record/locator，不发布文件。"""

    def __init__(
        self,
        store: ContextPlanDetailStore,
        source_snapshots: tuple[ForkDetailSourceSnapshot, ...] | None = None,
    ) -> None:
        self._store = store
        self._source_snapshots = (
            None
            if source_snapshots is None
            else {
                detail_ref_key(snapshot.source_record.detail_ref): snapshot
                for snapshot in source_snapshots
            }
        )
        if self._source_snapshots is not None and len(self._source_snapshots) != len(
            source_snapshots
        ):
            raise ValueError("fork detail snapshot 包含重复的 typed identity")

    def capture_source_detail(
        self,
        *,
        source_record: DetailRecord,
    ) -> ForkDetailSourceSnapshot:
        """在 source 仍受保护时完成认证，后续物化不得再读 source。"""
        if not isinstance(source_record, DetailRecord):
            raise TypeError("fork detail snapshot 必须使用 typed record")
        try:
            value = self._store.read(
                session_id=source_record.session_id,
                thread_id=source_record.thread_id,
                record=source_record,
                include_sensitive=source_record.sensitive,
            )
        except (RuntimeError, ValueError, TypeError, OSError):
            if source_record.required:
                raise DetailUnavailableError(
                    "detail-unavailable: fork source authentication/retention failed"
                ) from None
            return ForkDetailSourceSnapshot(source_record, None)
        return ForkDetailSourceSnapshot(
            source_record,
            canonical_json_bytes(value),
        )

    def prepare_detail(
        self,
        *,
        source_record: DetailRecord,
        target_ref: DetailRef,
        source_snapshot: ForkDetailSourceSnapshot | None = None,
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
        if source_snapshot is None and self._source_snapshots is not None:
            source_snapshot = self._source_snapshots.get(
                detail_ref_key(source_record.detail_ref)
            )
            if source_snapshot is None:
                raise DetailUnavailableError(
                    "source-mismatch: fork source snapshot 缺少 detail identity"
                )
        frozen_source = source_snapshot or self.capture_source_detail(
            source_record=source_record
        )
        if frozen_source.source_record != source_record:
            raise DetailUnavailableError(
                "source-mismatch: fork detail snapshot 与 registry record 不一致"
            )
        if frozen_source.value_bytes is None:
            raise DetailUnavailableError(
                "detail-unavailable: fork source authentication/retention failed"
            )
        value = json.loads(frozen_source.value_bytes)
        if not isinstance(value, dict):
            raise DetailUnavailableError(
                "source-mismatch: fork detail snapshot value 不是 JSON object"
            )
        body = value["detail"]
        raw, record = build_detail_payload(
            detail_ref=target_ref,
            detail_kind=source_record.detail_kind,
            retention_class=source_record.retention_class,
            visibility=source_record.visibility,
            stored_detail=body,
            source_revision=source_record.source_revision,
            length=source_record.length,
            detail_content_hash=value["detail_content_hash"],
            required=source_record.required,
            sensitive=source_record.sensitive,
            protection=source_record.protection,
            created_at=value["created_at"],
            expires_at=source_record.expires_at,
            checkpoint_ns=source_record.checkpoint_ns,
        )
        return ForkDetailArtifact(record, raw)
