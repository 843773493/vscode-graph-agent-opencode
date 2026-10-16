"""ResourceActivation 资源载荷的敏感 detail body owner。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)
from app.services.infrastructure.rollout_context.activation.common import (
    HASH_MISMATCH_CODE,
    ResourceActivationStoreError,
)
from app.services.infrastructure.rollout_context.activation.lineage import (
    ActivationLineageBodyStore,
)
from app.services.infrastructure.rollout_context.activation.reads import (
    binding_assembly_id,
)
from app.services.infrastructure.rollout_context.activation.schema import (
    require_resource_activation_schema,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    DetailUnavailableError,
    detail_record_from_mapping,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.store import (
        RolloutContextStore,
    )

PAYLOAD_DETAIL_KIND = "resource_activation_payload"
PAYLOAD_RETENTION_CLASS = "resource_activation_payload"
BODY_UNAVAILABLE_CODE = "resource-activation-body-unavailable"


class ResourceActivationBodyStore:
    """只管理 payload 正文；resource/ordinal/ref 关联仍由 activation catalog 持有。"""

    def __init__(
        self, storage: RolloutContextStore, detail_store: ContextPlanDetailStore
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
        self._storage.assemblies.register_context_plan_detail(record)
        return record.detail_ref

    def read_payload(
        self,
        *,
        snapshot: ResourceActivationSnapshotRef,
        binding: ResourceProvenanceRef,
        storage_owner: tuple[str, str],
        checkpoint_ns: str = "",
    ) -> object:
        """按 catalog 恢复出的 snapshot binding 校验并读取 payload。"""

        lineage_body = ActivationLineageBodyStore(self._storage, self._detail_store)
        with self._storage.storage._connect(
            storage_owner[1], checkpoint_ns, session_id=storage_owner[0], read_only=True,
        ) as connection:
            self._storage.storage._require_v2_runtime(connection)
            require_resource_activation_schema(connection)
            expected_assembly_id = binding_assembly_id(
                connection, snapshot, binding, storage_owner=storage_owner,
                read_lineage_manifest=lambda ref: lineage_body.read_lineage_manifest(
                    detail_ref=ref, checkpoint_ns=checkpoint_ns, storage_owner=storage_owner,
                ),
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
            raw = self._storage.assemblies.get_context_plan_detail(
                storage_owner[0],
                storage_owner[1],
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
                session_id=storage_owner[0],
                thread_id=storage_owner[1],
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
