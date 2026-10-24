"""activation source lineage 的精确 body store 适配（9.2）。

lineage manifest 不属于 SQLite catalog：本适配把 manifest 正文写入既有
``ContextPlanDetailStore``（单份精确 detail 文件）并登记其 manifest，catalog 只
保存 typed ``DetailRef`` 与 digest。这样 catalog 不能补造 lineage 向量，
retention 失效时也必须显式报 ``detail-unavailable``。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from app.core.atomic_fs import native_io_path
from app.domain.itemized.identity.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.assembly.detail_registry import (
    _COLUMNS,
    _detail_mapping,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
    detail_record_from_mapping,
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    parse_detail_payload,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)

LINEAGE_DETAIL_KIND = "resource_activation_lineage"
LINEAGE_RETENTION_CLASS = "resource_activation_provenance"


def _lineage_payload(value: Mapping[str, object], detail_kind: str) -> Mapping[str, object]:
    if detail_kind != LINEAGE_DETAIL_KIND:
        raise RuntimeError("source-mismatch: lineage detail kind 不属于 activation provenance")
    body = value.get("detail")
    if not isinstance(body, Mapping):
        raise TypeError("source-mismatch: lineage detail 正文不是 object")
    return body


def read_captured_lineage_manifest(
    connection: sqlite3.Connection, detail_ref: DetailRef, *, captured_node: Path,
) -> Mapping[str, object]:
    """只读 caller 捕获的 catalog 和正文，不经运行时路径解析器回读 live。"""
    require_historical_scope(connection, detail_ref.session_id, detail_ref.thread_id)
    row = connection.execute(
        f"SELECT {','.join(_COLUMNS)} FROM context_plan_details WHERE detail_ref=?",
        (detail_ref_key(detail_ref),),
    ).fetchone()
    if row is None:
        raise KeyError(f"detail-unavailable: captured lineage manifest 不存在: {detail_ref}")
    record = detail_record_from_mapping(_detail_mapping(tuple(row)))
    path = native_io_path(captured_node / detail_relative_path(detail_ref))
    if path.is_symlink() or not path.is_file():
        raise DetailUnavailableError(f"detail-unavailable: captured lineage 正文缺失: {detail_ref}")
    value, parsed = parse_detail_payload(path.read_bytes(), detail_ref=detail_ref)
    if parsed != record:
        raise DetailUnavailableError(f"source-mismatch: captured lineage manifest/hash 不一致: {detail_ref}")
    return _lineage_payload(value, record.detail_kind)


class ActivationLineageBodyStore:
    """以精确 detail 文件保存/读取 activation lineage manifest 的唯一适配。"""

    def __init__(self, storage, detail_store: ContextPlanDetailStore) -> None:
        self._storage = storage
        self._detail_store = detail_store

    def write_lineage_manifest(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str,
        manifest: Mapping[str, object],
    ) -> DetailRef:
        if not isinstance(manifest, Mapping):
            raise TypeError("lineage manifest 必须是 mapping")
        record = self._detail_store.write(
            session_id=owner_session_id,
            thread_id=owner_thread_id,
            assembly_id=activation_snapshot_id,
            detail_kind=LINEAGE_DETAIL_KIND,
            retention_class=LINEAGE_RETENTION_CLASS,
            visibility="internal",
            detail=dict(manifest),
            required=True,
            sensitive=True,
            checkpoint_ns=checkpoint_ns,
        )
        self._storage.assemblies.register_context_plan_detail(record)
        return record.detail_ref

    def read_lineage_manifest(
        self,
        *,
        detail_ref: DetailRef,
        checkpoint_ns: str,
        storage_owner: tuple[str, str],
        snapshot: RolloutReadSnapshot | None = None,
    ) -> Mapping[str, object]:
        raw = self._storage.assemblies.get_context_plan_detail(
            storage_owner[0],
            storage_owner[1],
            detail_ref=detail_ref,
            checkpoint_ns=checkpoint_ns,
            snapshot=snapshot,
        )
        record = detail_record_from_mapping(raw)
        if record.detail_kind != LINEAGE_DETAIL_KIND:
            raise RuntimeError("source-mismatch: lineage detail kind 不属于 activation provenance")
        payload = self._detail_store.read(
            session_id=storage_owner[0],
            thread_id=storage_owner[1],
            record=record,
            include_sensitive=True,
            snapshot=snapshot,
        )
        return _lineage_payload(payload, record.detail_kind)


__all__ = [
    "LINEAGE_DETAIL_KIND",
    "LINEAGE_RETENTION_CLASS",
    "ActivationLineageBodyStore",
]
