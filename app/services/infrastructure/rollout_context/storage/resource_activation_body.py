"""ResourceActivation 资源载荷的敏感 detail body owner。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    DetailUnavailableError,
    detail_record_from_mapping,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_common import (
    HASH_MISMATCH_CODE,
    ResourceActivationStoreError,
    _detail_ref_from_key,
    _lineage_by_resource,
    _member_bindings_for_resource,
    _source_lineage_ref_for_binding,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_lineage import (
    ActivationLineageBodyStore,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_schema import (
    require_resource_activation_schema,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )

PAYLOAD_DETAIL_KIND = "resource_activation_payload"
PAYLOAD_RETENTION_CLASS = "resource_activation_payload"
BODY_UNAVAILABLE_CODE = "resource-activation-body-unavailable"


class ResourceActivationBodyStore:
    """只管理 payload 正文；resource/ordinal/ref 关联仍由 activation catalog 持有。"""

    def __init__(
        self, storage: RolloutStorage, detail_store: ContextPlanDetailStore
    ) -> None:
        self._storage = storage
        self._detail_store = detail_store

    def write_resource_body(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        resource_id: str,
        activation_ordinal: int,
        payload: object,
        checkpoint_ns: str,
    ) -> DetailRef:
        """写入必需的 精确正文，返回供 activation catalog 登记的真实 typed ref。"""

        content_hash = sha256_jcs(payload)
        content_length = len(canonical_json_bytes(payload))
        record = self._detail_store.write(
            session_id=owner_session_id,
            thread_id=owner_thread_id,
            assembly_id=activation_snapshot_id,
            detail_kind=PAYLOAD_DETAIL_KIND,
            retention_class=PAYLOAD_RETENTION_CLASS,
            visibility="internal",
            detail=payload,
            required=True,
            sensitive=True,
            checkpoint_ns=checkpoint_ns,
        )
        record.detail_ref.require_owner(
            owner_session_id, owner_thread_id, activation_snapshot_id
        )
        if (
            record.detail_kind != PAYLOAD_DETAIL_KIND
            or record.retention_class != PAYLOAD_RETENTION_CLASS
            or record.required is not True
            or record.sensitive is not True
            or record.source_revision != content_hash
            or record.length != content_length
        ):
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                "activation payload detail manifest 与 canonical 正文不一致",
            )
        self._storage.register_context_plan_detail(record)
        return record.detail_ref

    def read_payload(
        self,
        *,
        snapshot: ResourceActivationSnapshotRef,
        binding: ResourceProvenanceRef,
        checkpoint_ns: str = "",
    ) -> object:
        """按 catalog 恢复出的 snapshot binding 校验并读取 payload。"""

        expected_assembly_id = self._binding_assembly_id(
            snapshot, binding, checkpoint_ns=checkpoint_ns
        )
        detail_ref = binding.detail_ref
        if detail_ref is None:
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                f"resource 没有 payload detail_ref: {binding.resource_id}",
            )
        detail_ref.require_owner(
            snapshot.owner_session_id,
            snapshot.owner_thread_id,
            expected_assembly_id,
        )
        if binding.content_hash is None:
            raise ResourceActivationStoreError(
                BODY_UNAVAILABLE_CODE,
                f"resource payload 没有可校验的 canonical content hash: {binding.resource_id}",
            )
        try:
            raw = self._storage.get_context_plan_detail(
                snapshot.owner_session_id,
                snapshot.owner_thread_id,
                detail_ref=detail_ref,
                checkpoint_ns=checkpoint_ns,
            )
            record = detail_record_from_mapping(raw)
            self._validate_record(
                record,
                detail_ref=detail_ref,
                owner_session_id=snapshot.owner_session_id,
                owner_thread_id=snapshot.owner_thread_id,
                activation_snapshot_id=expected_assembly_id,
                content_hash=binding.content_hash,
                content_length=binding.content_length,
                resource_id=binding.resource_id,
                activation_ordinal=binding.activation_ordinal,
            )
            value = self._detail_store.read(
                session_id=snapshot.owner_session_id,
                thread_id=snapshot.owner_thread_id,
                record=record,
                include_sensitive=True,
            )
        except (KeyError, DetailUnavailableError) as error:
            raise ResourceActivationStoreError(
                BODY_UNAVAILABLE_CODE,
                f"敏感 activation payload 正文不可用: {binding.resource_id}",
            ) from error

        payload = value.get("detail")
        if (
            sha256_jcs(payload) != binding.content_hash
            or len(canonical_json_bytes(payload)) != binding.content_length
        ):
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                "activation payload hash/length 与 catalog binding 不一致: "
                f"{binding.resource_id}#{binding.activation_ordinal}",
            )
        return payload

    def _binding_assembly_id(
        self,
        snapshot: ResourceActivationSnapshotRef,
        binding: ResourceProvenanceRef,
        *,
        checkpoint_ns: str,
    ) -> str:
        if not isinstance(snapshot, ResourceActivationSnapshotRef) or not isinstance(
            binding, ResourceProvenanceRef
        ):
            raise TypeError("payload 读取需要 typed activation snapshot 与 binding")
        by_resource = tuple(
            item
            for item in snapshot.bindings
            if item.resource_id == binding.resource_id
        )
        by_ordinal = tuple(
            item
            for item in snapshot.bindings
            if item.activation_ordinal == binding.activation_ordinal
        )
        if by_resource != (binding,) or by_ordinal != (binding,):
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                "payload binding 的 resource_id/activation_ordinal 不属于 snapshot: "
                f"{binding.resource_id}#{binding.activation_ordinal}",
            )
        if (
            snapshot.snapshot_kind == "model_call"
            and snapshot.parent is not None
            and binding in snapshot.parent.bindings
        ):
            root = snapshot.parent
        else:
            root = snapshot
        ref = binding.detail_ref
        if ref is None or ref.assembly_id == root.activation_snapshot_id:
            return root.activation_snapshot_id
        if root.turn_id is None or root.snapshot_kind != "execution":
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE, "internal/model_call binding 不得借用其它冻结 snapshot 正文"
            )
        # 同真实用户 Turn 的新 execution 关联可以逐字节复用原 binding/detail；
        # 必须由唯一 catalog 验证来源 root，不能复制正文或冒充新 owner。
        with self._storage._connect(
            root.owner_thread_id, checkpoint_ns, session_id=root.owner_session_id, read_only=True,
        ) as connection:
            self._storage._require_v2_runtime(connection)
            require_resource_activation_schema(connection)
            row = connection.execute(
                "SELECT b.resource_id, b.activation_ordinal, b.display_uri, b.resource_kind, b.owner_scope, b.facet, b.revision, b.availability, b.content_length, b.content_hash, b.effective_boundary, b.captured_registry_generation, b.source_lineage_digest, b.detail_ref, s.lineage_detail_ref, s.lineage_manifest_digest FROM resource_activation_snapshots s JOIN resource_activation_bindings b ON b.session_id = s.session_id AND b.thread_id = s.thread_id AND b.activation_snapshot_id = s.activation_snapshot_id WHERE s.session_id = ? AND s.thread_id = ? AND s.snapshot_kind = 'execution' AND s.activation_snapshot_id = ? AND s.turn_id = ? AND b.resource_id = ? AND b.activation_ordinal = ?",
                (root.owner_session_id, root.owner_thread_id, ref.assembly_id, root.turn_id,
                 binding.resource_id, binding.activation_ordinal),
            ).fetchone()
        from app.services.infrastructure.rollout_context.assembly.detail_identity import (
            detail_ref_key,
        )
        expected = (
            binding.resource_id, binding.activation_ordinal, binding.display_uri,
            binding.resource_kind, binding.owner_scope, binding.facet, binding.revision,
            binding.availability, binding.content_length, binding.content_hash,
             binding.effective_boundary,
            binding.captured_registry_generation, binding.source_lineage_digest,
            detail_ref_key(ref),
        )
        if row is None or row[:-2] != expected:
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE, "retry binding 的原冻结正文关系不一致"
            )
        lineage_ref = _detail_ref_from_key(row[-2], field="lineage_detail_ref")
        lineage_ref.require_owner(root.owner_session_id, root.owner_thread_id, ref.assembly_id)
        manifest = ActivationLineageBodyStore(
            self._storage, self._detail_store,
        ).read_lineage_manifest(detail_ref=lineage_ref, checkpoint_ns=checkpoint_ns)
        if sha256_jcs(manifest) != row[-1]:
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE, "retry 原冻结 lineage manifest digest 不一致",
            )
        lineages = _lineage_by_resource(manifest)
        lineage = _source_lineage_ref_for_binding(
            lineages, resource_id=binding.resource_id,
            expected_ordinal=binding.activation_ordinal,
            expected_digest=binding.source_lineage_digest,
        )
        if (
            lineage != binding.source_lineage_ref
            or _member_bindings_for_resource(lineages, resource_id=binding.resource_id) != binding.members
        ):
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE, "retry binding 的完整 protected 成员/lineage 事实不一致",
            )
        return ref.assembly_id

    @staticmethod
    def _validate_record(
        record: DetailRecord,
        *,
        detail_ref: DetailRef,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        content_hash: str,
        content_length: int,
        resource_id: str,
        activation_ordinal: int,
    ) -> None:
        detail_ref.require_owner(
            owner_session_id, owner_thread_id, activation_snapshot_id
        )
        if record.detail_ref != detail_ref:
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                f"payload detail ref 与 binding 不一致: {resource_id}#{activation_ordinal}",
            )
        if record.status != "available" or record.availability != "available":
            raise ResourceActivationStoreError(
                BODY_UNAVAILABLE_CODE,
                f"activation payload detail 已不可用: {resource_id}#{activation_ordinal}",
            )
        if (
            record.session_id != owner_session_id
            or record.thread_id != owner_thread_id
            or record.assembly_id != activation_snapshot_id
            or record.detail_kind != PAYLOAD_DETAIL_KIND
            or record.retention_class != PAYLOAD_RETENTION_CLASS
            or record.required is not True
            or record.sensitive is not True
            or record.source_revision != content_hash
            or record.length != content_length
        ):
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                "payload detail owner/hash/length manifest 与 catalog binding 不一致: "
                f"{resource_id}#{activation_ordinal}",
            )


__all__ = [
    "BODY_UNAVAILABLE_CODE",
    "PAYLOAD_DETAIL_KIND",
    "PAYLOAD_RETENTION_CLASS",
    "ResourceActivationBodyStore",
]
