"""activation catalog 的行编码、错误码与受保护 lineage manifest 投影（9.2）。

writer（``resource_activation_store``）与 reader（``resource_activation_reads``）
共享同一份列序、typed detail key 编码与 lineage manifest digest 语义。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final, Protocol

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    SourceLineageRef,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
)

SCHEMA_UNAVAILABLE_CODE: Final = "resource-activation-schema-unavailable"
LINEAGE_MANIFEST_SCHEMA: Final = "resource-activation-lineage-manifest:v1"

SNAPSHOT_COLUMNS: Final = (
    "session_id",
    "thread_id",
    "activation_snapshot_id",
    "snapshot_kind",
    "parent_turn_snapshot_id",
    "activation_policy_revision",
    "activation_policy_hash",
    "registry_generation",
    "turn_id",
    "model_call_id",
    "captured_at",
    "bindings_hash",
    "activation_provenance_hash",
    "binding_count",
    "lineage_manifest_digest",
    "lineage_detail_ref",
    "created_at",
)

BINDING_COLUMNS: Final = (
    "session_id",
    "thread_id",
    "activation_snapshot_id",
    "activation_ordinal",
    "resource_id",
    "display_uri",
    "resource_kind",
    "owner_scope",
    "facet",
    "revision",
    "availability",
    "content_length",
    "content_hash",
    "redacted_stable_digest",
    "effective_boundary",
    "captured_registry_generation",
    "source_lineage_digest",
    "snapshot_ref",
    "detail_ref",
)

ASSEMBLY_BINDING_COLUMNS: Final = (
    "assembly_id",
    "session_id",
    "thread_id",
    "activation_snapshot_id",
    "plan_id",
    "plan_hash",
    "request_hash",
    "selection_manifest_hash",
    "bindings_hash",
    "activation_provenance_hash",
    "bound_at",
)


class ResourceActivationStoreError(RuntimeError):
    """activation storage 的显式失败；``code`` 是闭合错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


class ResourceActivationLineageBodyStore(Protocol):
    """受保护 lineage body store：catalog 只保存 ref/digest，正文在此。"""

    def write_lineage_manifest(
        self,
        *,
        owner_session_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str,
        manifest: Mapping[str, object],
    ) -> DetailRef: ...

    def read_lineage_manifest(
        self,
        *,
        detail_ref: DetailRef,
        checkpoint_ns: str,
    ) -> Mapping[str, object]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _optional_detail_ref_from_key(value: object, *, field: str) -> DetailRef | None:
    return None if value is None else _detail_ref_from_key(value, field=field)


def _detail_ref_key(ref: DetailRef, *, field: str) -> str:
    """复用 assembly 的唯一 typed detail key 编码；不接受裸 ID 或路径。"""

    if not isinstance(ref, DetailRef):
        raise ResourceActivationStoreError(
            "resource-activation-schema-invalid", f"{field} 必须是 typed DetailRef"
        )
    return detail_ref_key(ref)


def _detail_ref_from_key(value: object, *, field: str) -> DetailRef:
    return detail_ref_from_key(strict_text(value, field=field))


def lineage_manifest(snapshot: ResourceActivationSnapshotRef) -> dict[str, object]:
    """来源 manifest 的受保护投影：source 向量、derivation 版本与 digest。"""

    return {
        "schema": LINEAGE_MANIFEST_SCHEMA,
        "activation_snapshot_id": snapshot.activation_snapshot_id,
        "lineages": [
            {
                "resource_id": binding.resource_id,
                "activation_ordinal": binding.activation_ordinal,
                "source_lineage_ref": binding.source_lineage_ref.to_dict(),
                "source_lineage_digest": binding.source_lineage_digest,
            }
            for binding in snapshot.bindings
        ],
    }


def lineage_manifest_digest(snapshot: ResourceActivationSnapshotRef) -> str:
    """由内存 snapshot 确定性导出 lineage manifest digest。"""

    return sha256_jcs(lineage_manifest(snapshot))


def _lineage_by_resource(
    manifest: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    if not isinstance(manifest, Mapping) or manifest.get("schema") != (
        LINEAGE_MANIFEST_SCHEMA
    ):
        raise ResourceActivationStoreError(
            "resource-activation-lineage-invalid",
            "受保护 lineage manifest schema 不匹配",
        )
    entries = manifest.get("lineages")
    if not isinstance(entries, list) or not entries:
        raise ResourceActivationStoreError(
            "resource-activation-lineage-invalid",
            "受保护 lineage manifest 缺少 lineages",
        )
    result: dict[str, Mapping[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ResourceActivationStoreError(
                "resource-activation-lineage-invalid", "lineage entry 必须是对象"
            )
        resource_id = strict_text(
            entry.get("resource_id"), field="lineage.resource_id"
        )
        if resource_id in result:
            raise ResourceActivationStoreError(
                "resource-activation-lineage-invalid",
                f"lineage manifest 重复 resource_id: {resource_id}",
            )
        result[resource_id] = entry
    return result


def _source_lineage_ref_for_binding(
    lineages: Mapping[str, Mapping[str, object]],
    *,
    resource_id: str,
    expected_ordinal: int,
    expected_digest: str,
) -> SourceLineageRef:
    entry = lineages.get(resource_id)
    if entry is None:
        raise ResourceActivationStoreError(
            "resource-activation-lineage-invalid",
            f"binding 缺失受保护 lineage manifest: {resource_id}",
        )
    ordinal = strict_non_negative_int(
        entry.get("activation_ordinal"), field="lineage.activation_ordinal"
    )
    if ordinal != expected_ordinal:
        raise ResourceActivationStoreError(
            "resource-activation-lineage-invalid",
            f"lineage ordinal 与 binding 不一致: {resource_id}",
        )
    lineage = SourceLineageRef.from_dict(entry.get("source_lineage_ref"))
    if entry.get("source_lineage_digest") != lineage.digest:
        raise ResourceActivationStoreError(
            "resource-activation-hash-mismatch",
            f"lineage digest 与 lineage ref 不一致: {resource_id}",
        )
    if lineage.digest != expected_digest:
        raise ResourceActivationStoreError(
            "resource-activation-hash-mismatch",
            f"lineage digest 与 binding 列不一致: {resource_id}",
        )
    return lineage
