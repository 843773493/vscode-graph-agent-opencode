"""ResourceActivation 资源载荷的敏感 detail body owner。"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import ExitStack, nullcontext
from typing import TYPE_CHECKING

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
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
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

    def write_resource_bodies(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        payloads: Sequence[object],
        checkpoint_ns: str,
    ) -> tuple[tuple[DetailRef, str, int], ...]:
        """批量发布冻结正文；同次构造的 hash/长度直接交给 activation binding。"""
        records: list[DetailRecord] = []
        with ExitStack() as rollback:
            for payload in payloads:
                prepared = self._detail_store.prepare(
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
                paths = self._detail_store.publish(prepared)
                rollback.callback(self._detail_store.discard_published, paths)
                records.append(prepared.record)
            # SQL 提交后的关闭也可能报错；进入登记后保留正文，不能因
            # 提交结果未知而删除已经被 catalog 引用的文件。
            rollback.pop_all()
            self._storage.assemblies.register_context_plan_details(records)
        return tuple((record.detail_ref, record.source_revision, record.length) for record in records)

    def read_payload(
        self,
        *,
        snapshot: ResourceActivationSnapshotRef,
        binding: ResourceProvenanceRef,
        storage_owner: tuple[str, str],
        checkpoint_ns: str = "",
        read_snapshot: RolloutReadSnapshot | None = None,
    ) -> object:
        """按 catalog 恢复出的 snapshot binding 校验并读取 payload。"""

        lineage_body = ActivationLineageBodyStore(self._storage, self._detail_store)
        # binding、重试 lineage、manifest 与正文共用当前读取快照，避免各层重开连接。
        with (
            self._storage.storage.open_read_snapshot(*storage_owner, checkpoint_ns)
            if read_snapshot is None else nullcontext(read_snapshot)
        ) as current, self._storage.storage.read_connection(
            *storage_owner, checkpoint_ns, snapshot=current,
        ) as connection:
            require_resource_activation_schema(connection)
            expected_assembly_id = binding_assembly_id(
                connection, snapshot, binding, storage_owner=storage_owner,
                read_lineage_manifest=lambda ref: lineage_body.read_lineage_manifest(
                    detail_ref=ref, checkpoint_ns=checkpoint_ns, storage_owner=storage_owner,
                    snapshot=current,
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
                record = self._storage.assemblies.get_context_plan_detail(
                    storage_owner[0],
                    storage_owner[1],
                    detail_ref=detail_ref,
                    checkpoint_ns=checkpoint_ns,
                    snapshot=current,
                )
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
                    snapshot=current,
                )
            except (KeyError, DetailUnavailableError) as error:
                raise ResourceActivationStoreError(
                    BODY_UNAVAILABLE_CODE,
                    f"敏感 activation payload 正文不可用: {binding.resource_id}",
                ) from error

            # detail parser 已逐字节核验正文摘要、长度及 envelope 与 SQL manifest；
            # 长度已与 binding 对齐，直接比对已验证摘要，避免再次编码同一正文。
            if value["detail_content_hash"] != binding.content_hash:
                raise ResourceActivationStoreError(
                    HASH_MISMATCH_CODE,
                    "activation payload hash/length 与 catalog binding 不一致: "
                    f"{binding.resource_id}#{binding.activation_ordinal}",
                )
            return value["detail"]

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
