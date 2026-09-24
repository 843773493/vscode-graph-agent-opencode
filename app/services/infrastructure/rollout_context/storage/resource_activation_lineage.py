"""activation source lineage 的受保护 body store 适配（9.2）。

lineage manifest 不属于 SQLite catalog：本适配把 manifest 正文写入既有
``ContextPlanDetailStore``（受保护 detail 文件）并登记其 manifest，catalog 只
保存 typed ``DetailRef`` 与 digest。这样 catalog 不能补造 lineage 向量，
retention 失效时也必须显式报 ``detail-unavailable``。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.itemized.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_record_from_mapping,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)

LINEAGE_DETAIL_KIND = "resource_activation_lineage"
LINEAGE_RETENTION_CLASS = "resource_activation_provenance"


class ActivationLineageBodyStore:
    """以受保护 detail 文件保存/读取 activation lineage manifest 的唯一适配。"""

    def __init__(self, storage, detail_store: ContextPlanDetailStore) -> None:
        self._storage = storage
        self._detail_store = detail_store

    def write_lineage_manifest(
        self,
        *,
        owner_session_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str,
        manifest: Mapping[str, object],
    ) -> DetailRef:
        if not isinstance(manifest, Mapping):
            raise TypeError("lineage manifest 必须是 mapping")
        if not getattr(
            self._detail_store, "supports_protected_details", False
        ):
            raise RuntimeError(
                "resource-activation-schema-unavailable: lineage manifest 需要 "
                "protected detail backend"
            )
        record = self._detail_store.write(
            session_id=owner_session_id,
            assembly_id=activation_snapshot_id,
            detail_kind=LINEAGE_DETAIL_KIND,
            retention_class=LINEAGE_RETENTION_CLASS,
            visibility="internal",
            detail=dict(manifest),
            required=True,
            sensitive=True,
            checkpoint_ns=checkpoint_ns,
        )
        self._storage.register_context_plan_detail(record)
        return record.detail_ref

    def read_lineage_manifest(
        self,
        *,
        detail_ref: DetailRef,
        checkpoint_ns: str,
    ) -> Mapping[str, object]:
        raw = self._storage.get_context_plan_detail(
            detail_ref.session_id,
            detail_ref=detail_ref,
            checkpoint_ns=checkpoint_ns,
        )
        record = detail_record_from_mapping(raw)
        if record.detail_kind != LINEAGE_DETAIL_KIND:
            raise RuntimeError(
                "source-mismatch: lineage detail kind 不属于 activation provenance"
            )
        payload = self._detail_store.read(
            session_id=detail_ref.session_id,
            record=record,
            include_sensitive=True,
        )
        body = payload.get("detail")
        if not isinstance(body, Mapping):
            raise TypeError("source-mismatch: lineage detail 正文不是 object")
        return body


__all__ = [
    "LINEAGE_DETAIL_KIND",
    "LINEAGE_RETENTION_CLASS",
    "ActivationLineageBodyStore",
]
