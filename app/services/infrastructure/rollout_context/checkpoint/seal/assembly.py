"""Saver 的低层 snapshot seal 边界；只接受真实 runtime registration。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Protocol

from app.agents.providers.wire_finalize import (
    WIRE_PROJECTOR_ID,
    WIRE_PROJECTOR_VERSION,
    ChatWireProfile,
    ResponsesWireProfile,
    finalize_provider_wire,
)
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.hash.request_hash import _context_request_hash_preimage
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    canonical_json_bytes_and_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.refs import ref_identity
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    _ValidatedPredecessors,
)
from app.services.infrastructure.rollout_context.assembly.plans.types import (
    ContextAssemblySealReceipt,
    ContextPlanRegistration,
)
from app.services.infrastructure.rollout_context.assembly.seal_preflight import (
    canonical_tool_pairings,
    validate_seal_dispatch_invariants,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_projection import (
    ContextProjectionResult,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.cleanup import (
    record_failed_seal,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    require_key,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    DetailUnavailableError,
    PreparedDetail,
    detail_record_from_mapping,
)
from app.services.mapping.itemized.projection import build_projection_evidence


class ActivationSealBinding(Protocol):
    """assembly 事务内的 activation 绑定参与者（唯一 Saver 提供）。

    只在既有 ``assembly_sealed`` 事务内写 activation snapshot/binding 行；
    不读取当前资源、不做源 I/O。
    """

    @property
    def snapshot(self) -> ResourceActivationSnapshotRef: ...

    def bind_assembly(
        self,
        connection,
        *,
        assembly_id: str,
        plan_id: str,
        plan_hash: str,
        request_hash: str,
        selection_manifest_hash: str,
    ) -> None: ...


def _project_wire_snapshot(
    owner,
    snapshot: ContextAssemblySnapshot,
    sealed_plan: ContextRequestPlan,
    items: dict[tuple[str, str, str], CanonicalItemRecord],
    prepared_details: tuple[PreparedDetail, ...],
    wire_profile: ChatWireProfile | ResponsesWireProfile,
    wire_params: Mapping[str, object],
    checkpoint_ns: str,
    activation: ResourceActivationSnapshotRef | None,
) -> tuple[ContextAssemblySnapshot, ContextProjectionResult]:
    """在 authority 读取已关闭后生成本次 wire；不重读 canonical 正文。"""
    bodies = owner._request_content_for_plan(
        snapshot.session_id, snapshot.thread_id, checkpoint_ns, sealed_plan, None,
        prepared_details=prepared_details,
    )
    projection = owner._project_context_projection(
        snapshot.session_id, snapshot.thread_id, snapshot, items, bodies,
        resource_activation=activation,
        include_runtime_notices=True, include_summaries=True,
        target_format=snapshot.target_format,
        include_native=isinstance(wire_profile, ResponsesWireProfile),
    )
    preimage = normalize_wire_request(finalize_provider_wire(
        messages=projection.messages, tools=projection.tools,
        native_request=None if projection.native is None else projection.native["request"],
        profile=wire_profile, params=wire_params,
    ))
    # snapshot 来自本次 composer，只替换 wire；冷读仍独立验证所有 hash。
    snapshot = replace(
        snapshot, request_hash_preimage=preimage,
        request_hash=sha256_jcs(_context_request_hash_preimage(
            sealed_plan, snapshot.provider_version,
            projector_id=snapshot.projector_id, projector_version=snapshot.projector_version,
            target_format=snapshot.target_format, wire_request=preimage,
            recomputed_plan_hash=snapshot.plan_hash,
        )),
    )
    return snapshot, replace(projection, assembly=snapshot)


def _activation_binding_callable(
    activation_binding: ActivationSealBinding | None,
    snapshot: ContextAssemblySnapshot,
):
    """把 activation 绑定参与者包装成 assembly 事务内的单次回调。

    selection manifest hash 由同一 sealed plan 纯映射导出（无 I/O），必须与
    assembly 的 plan/request hash 在同一次提交里绑定。
    """

    if activation_binding is None:
        return None
    sealed_plan = snapshot.as_sealed_plan()
    evidence = build_projection_evidence(sealed_plan, projection="assembly-seal")

    def _bind(connection) -> None:
        activation_binding.bind_assembly(
            connection,
            assembly_id=snapshot.assembly_id,
            plan_id=snapshot.plan_id,
            plan_hash=snapshot.plan_hash,
            request_hash=snapshot.request_hash,
            selection_manifest_hash=evidence.selection_manifest_hash,
        )

    return _bind


def seal_snapshot(
    owner,
    session_id: str,
    thread_id: str,
    snapshot: ContextAssemblySnapshot,
    *,
    registered: ContextPlanRegistration,
    seal_idempotency_key: str,
    seal_input_hash: str,
    detail: Mapping[str, object] | None,
    required_detail: bool,
    sensitive_detail: bool,
    checkpoint_ns: str,
    wire: tuple[ChatWireProfile | ResponsesWireProfile, Mapping[str, object]] | None,
    activation_binding: ActivationSealBinding | None = None,
    prepared_details: tuple[PreparedDetail, ...] = (),
    validated_predecessors: _ValidatedPredecessors | None = None,
) -> tuple[ContextAssemblySealReceipt, ContextProjectionResult | None]:
    source_refs = tuple(
        entry.ref for entry in snapshot.selection
        if entry.ref.ref_type == "canonical_item" and (
            entry.included or snapshot.selection_policy == "summary_candidate"
        )
    )
    tool_refs = tuple(
        entry.ref for entry in snapshot.selection
        if entry.included and entry.ref.ref_type == "canonical_item"
        and entry.ref.semantic_kind in (SemanticKind.TOOL_CALL.value, SemanticKind.TOOL_RESULT.value)
    )
    tool_keys = frozenset(ref_identity(ref) for ref in tool_refs)
    items: dict[tuple[str, str, str], CanonicalItemRecord] = {}
    projection = None
    if wire is not None:
        try:
            sealed_plan = snapshot.as_sealed_plan()
            if snapshot.seal_binding is None:
                raise ValueError("source-mismatch: wire seal 缺少已绑定 prefix")
            # wire 首次投影、tool preflight 与 source 验证只消费本次 fresh 读。
            # read_ref_items 返回前已关闭读 snapshot，后续 projector 不持有读锁。
            items = owner._storage.storage.read_ref_items(
                session_id, thread_id, refs=source_refs, checkpoint_ns=checkpoint_ns,
            )
            snapshot, projection = _project_wire_snapshot(
                owner, snapshot, sealed_plan, items, prepared_details, wire[0], wire[1], checkpoint_ns,
                activation_binding.snapshot if activation_binding is not None else None,
            )
        except BaseException as error:
            record_failed_seal(
                owner, session_id, thread_id, snapshot.plan_id,
                seal_idempotency_key, checkpoint_ns, error,
            )
            raise
    if (snapshot.session_id, snapshot.thread_id) != (session_id, thread_id):
        raise ValueError("source-mismatch: assembly seal owner 不一致")
    if (snapshot.projector_id, snapshot.projector_version) == (
        WIRE_PROJECTOR_ID, WIRE_PROJECTOR_VERSION,
    ) and activation_binding is None:
        raise ValueError("resource-activation-unavailable: wire assembly seal 缺少冻结 activation 绑定")
    require_key(seal_idempotency_key, field="seal_idempotency_key")
    require_key(seal_input_hash, field="seal_input_hash")
    # 已提交 assembly 的幂等重试不重新绑定 activation：旧 assembly 若在接入前
    # 封存，其 activation 事实必须由显式迁移 quarantine 表达，不得在重试时补造。
    bind_activation = _activation_binding_callable(activation_binding, snapshot)
    if wire is None and tool_refs:
        # 直接 snapshot seal 保留 tool-first：非工具正文错误不得抢先于 preflight。
        items = owner._storage.storage.read_ref_items(
            session_id, thread_id, refs=tool_refs, checkpoint_ns=checkpoint_ns,
        )
    # seal 与已提交 retry 都必须先通过唯一 preflight；冲突 fail closed，
    # 不进入字节比对或 detail 写入。
    validate_seal_dispatch_invariants(
        snapshot,
        tool_pairings=canonical_tool_pairings({key: item for key, item in items.items() if key in tool_keys}),
    )
    if (registered.session_id, registered.thread_id, registered.draft.plan_id) != (
        snapshot.session_id, snapshot.thread_id, snapshot.plan_id,
    ):
        raise ValueError("source-mismatch: snapshot 与已注册 draft owner 不一致")
    if registered.plan_state == "sealed":
        if prepared_details:
            raise ValueError("assembly-idempotency-conflict: sealed retry 不能准备新 detail")
        if (
            registered.seal_idempotency_key != seal_idempotency_key
            or registered.seal_input_hash != seal_input_hash
        ):
            raise ValueError(
                "assembly-idempotency-conflict: snapshot seal key/input 不一致"
            )
        committed = owner._storage.assemblies.get_context_assembly(
            session_id,
            thread_id,
            assembly_id=registered.assembly_id,
            checkpoint_ns=checkpoint_ns,
        )
        if canonical_json_bytes(committed.to_dict()) != canonical_json_bytes(
            snapshot.to_dict()
        ):
            raise ValueError(
                "assembly-idempotency-conflict: snapshot 与已提交 assembly 不一致"
            )
        detail_ref = owner._storage.assemblies.get_context_assembly_detail_ref(
            session_id,
            thread_id,
            assembly_id=committed.assembly_id,
            checkpoint_ns=checkpoint_ns,
        )
        if (detail is None) != (detail_ref is None):
            raise ValueError(
                "assembly-idempotency-conflict: 重试 header detail 存在性不一致"
            )
        if detail_ref is not None:
            record = detail_record_from_mapping(
                owner._storage.assemblies.get_context_plan_detail(
                    session_id,
                    thread_id,
                    detail_ref=detail_ref,
                    checkpoint_ns=checkpoint_ns,
                )
            )
            if record.required != required_detail or record.sensitive != sensitive_detail:
                raise ValueError(
                    "assembly-idempotency-conflict: 重试 header detail manifest 不一致"
                )
            canonical_detail_bytes, detail_hash = canonical_json_bytes_and_hash(detail)
            if (
                record.source_revision != detail_hash
                or record.length != len(canonical_detail_bytes)
            ):
                raise ValueError(
                    "assembly-idempotency-conflict: 重试 header detail manifest 不一致"
                )
            owner.read_context_plan_detail(
                session_id,
                thread_id,
                detail_ref=detail_ref,
                checkpoint_ns=checkpoint_ns,
            )
        elif required_detail or sensitive_detail:
            raise ValueError(
                "assembly-idempotency-conflict: 无正文的 header detail flags 不一致"
            )
        return owner._storage.assemblies.seal_context_assembly(
            session_id,
            thread_id,
            committed,
            seal_idempotency_key=seal_idempotency_key,
            seal_input_hash=seal_input_hash,
            detail_ref=detail_ref,
            checkpoint_ns=checkpoint_ns,
        ), None
    try:
        if wire is None:
            # tool 读取后至此只有纯 preflight；同次局部值不重复解码。
            # 空补集仍经过 fresh reader，保留原 source authority 边界。
            items.update(owner._storage.storage.read_ref_items(
                session_id, thread_id, checkpoint_ns=checkpoint_ns,
                refs=tuple(ref for ref in source_refs if ref_identity(ref) not in tool_keys),
            ))
        owner._validate_snapshot_sources(
            snapshot, checkpoint_ns, items=items, prepared_details=prepared_details,
        )
        if required_detail and detail is None:
            raise DetailUnavailableError(
                "detail-unavailable: required assembly detail missing"
            )
        if sensitive_detail and detail is None:
            raise ValueError("detail-unavailable: sensitive detail 必须提供正文")
        header = None
        if detail is not None:
            header = owner._detail_store.prepare(
                session_id=session_id,
                thread_id=thread_id,
                assembly_id=snapshot.assembly_id,
                detail_kind="assembly_snapshot",
                retention_class="assembly_audit",
                visibility="internal",
                detail=detail,
                required=required_detail,
                sensitive=sensitive_detail,
                checkpoint_ns=checkpoint_ns,
            )
        receipt = owner._storage.assemblies.seal_context_assembly(
            session_id,
            thread_id,
            snapshot,
            seal_idempotency_key=seal_idempotency_key,
            seal_input_hash=seal_input_hash,
            detail_ref=header.record.detail_ref if header is not None else None,
            checkpoint_ns=checkpoint_ns,
            activation_binding=bind_activation,
            prepared_details=prepared_details + ((header,) if header is not None else ()),
            detail_store=owner._detail_store,
            validated_predecessors=validated_predecessors,
        )
        return receipt, projection
    except BaseException as error:
        record_failed_seal(
            owner,
            session_id,
            thread_id,
            snapshot.plan_id,
            seal_idempotency_key,
            checkpoint_ns,
            error,
        )
        raise
