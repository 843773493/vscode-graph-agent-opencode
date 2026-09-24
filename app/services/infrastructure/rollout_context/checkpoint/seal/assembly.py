"""Saver 的低层 snapshot seal 边界；只接受真实 runtime registration。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.enums import SemanticKind
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    validate_sealed_plan,
)
from app.services.infrastructure.rollout_context.assembly.seal_preflight import (
    canonical_tool_pairings,
    validate_seal_dispatch_invariants,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.cleanup import (
    record_failed_seal,
    release_uncommitted_details,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    require_key,
    runtime_draft,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    DetailUnavailableError,
    detail_record_from_mapping,
)
from app.services.mapping.itemized.projection import build_projection_evidence


class ActivationSealBinding(Protocol):
    """assembly 事务内的 activation 绑定参与者（唯一 Saver 提供）。

    只在既有 ``assembly_sealed`` 事务内写 activation snapshot/binding 行；
    不读取当前资源、不做源 I/O。
    """

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

def _seal_tool_pairings(
    owner,
    snapshot: ContextAssemblySnapshot,
    checkpoint_ns: str,
) -> tuple[tuple[str, str], ...]:
    """唯一 seal owner 边界的显式 tool 配对来源。

    只从已提交 canonical items 的显式 tool_call_id 派生；assembly 不含
    included tool 条目时不读取 storage，直接返回空配对。
    """
    entries = tuple(
        entry
        for entry in snapshot.selection
        if entry.included
        and entry.ref.ref_type == "canonical_item"
        and entry.ref.semantic_kind
        in (SemanticKind.TOOL_CALL.value, SemanticKind.TOOL_RESULT.value)
    )
    if not entries:
        return ()
    items = owner._storage.read_items(
        snapshot.session_id,
        checkpoint_ns=checkpoint_ns,
        item_ids=tuple(entry.ref.ref_id for entry in entries),
    )
    return canonical_tool_pairings(items)


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
    snapshot: ContextAssemblySnapshot,
    *,
    seal_idempotency_key: str,
    seal_input_hash: str,
    detail: Mapping[str, object] | None,
    required_detail: bool,
    sensitive_detail: bool,
    checkpoint_ns: str,
    activation_binding: ActivationSealBinding | None = None,
) -> int:

    require_key(seal_idempotency_key, field="seal_idempotency_key")
    require_key(seal_input_hash, field="seal_input_hash")
    # 已提交 assembly 的幂等重试不重新绑定 activation：旧 assembly 若在接入前
    # 封存，其 activation 事实必须由显式迁移 quarantine 表达，不得在重试时补造。
    bind_activation = _activation_binding_callable(activation_binding, snapshot)
    # seal 与已提交 retry 都必须先通过唯一 preflight；冲突 fail closed，
    # 不进入字节比对或 detail 写入。
    validate_seal_dispatch_invariants(
        snapshot,
        tool_pairings=_seal_tool_pairings(owner, snapshot, checkpoint_ns),
    )
    registered = owner.get_context_plan_registration(
        snapshot.session_id, plan_id=snapshot.plan_id, checkpoint_ns=checkpoint_ns
    )
    draft = runtime_draft(registered)
    if registered.plan_state == "sealed":
        if (
            registered.seal_idempotency_key != seal_idempotency_key
            or registered.seal_input_hash != seal_input_hash
        ):
            raise ValueError(
                "assembly-idempotency-conflict: snapshot seal key/input 不一致"
            )
        committed = owner._storage.get_context_assembly(
            snapshot.session_id,
            assembly_id=registered.assembly_id,
            checkpoint_ns=checkpoint_ns,
        )
        if canonical_json_bytes(committed.to_dict()) != canonical_json_bytes(
            snapshot.to_dict()
        ):
            raise ValueError(
                "assembly-idempotency-conflict: snapshot 与已提交 assembly 不一致"
            )
        detail_ref = owner._storage.get_context_assembly_detail_ref(
            snapshot.session_id,
            assembly_id=committed.assembly_id,
            checkpoint_ns=checkpoint_ns,
        )
        if (detail is None) != (detail_ref is None):
            raise ValueError(
                "assembly-idempotency-conflict: 重试 header detail 存在性不一致"
            )
        if detail_ref is not None:
            record = detail_record_from_mapping(
                owner._storage.get_context_plan_detail(
                    snapshot.session_id,
                    detail_ref=detail_ref,
                    checkpoint_ns=checkpoint_ns,
                )
            )
            if (
                record.required != required_detail
                or record.sensitive != sensitive_detail
                or record.source_revision != sha256_jcs(detail)
                or record.length != len(canonical_json_bytes(detail))
            ):
                raise ValueError(
                    "assembly-idempotency-conflict: 重试 header detail manifest 不一致"
                )
            owner.read_context_plan_detail(
                snapshot.session_id, detail_ref=detail_ref, checkpoint_ns=checkpoint_ns
            )
        elif required_detail or sensitive_detail:
            raise ValueError(
                "assembly-idempotency-conflict: 无正文的 header detail flags 不一致"
            )
        return owner._storage.seal_context_assembly(
            committed,
            seal_idempotency_key=seal_idempotency_key,
            seal_input_hash=seal_input_hash,
            detail_ref=detail_ref,
            checkpoint_ns=checkpoint_ns,
        )
    record = None
    try:
        validate_sealed_plan(draft, snapshot)
        owner._validate_snapshot_sources(snapshot, checkpoint_ns)
        if required_detail and detail is None:
            raise DetailUnavailableError(
                "detail-unavailable: required assembly detail missing"
            )
        if (
            required_detail
            and sensitive_detail
            and not owner._detail_store.supports_protected_details
        ):
            raise DetailUnavailableError(
                "detail-unavailable: required detail 需要 protected storage backend"
            )
        if sensitive_detail and detail is None:
            raise ValueError("detail-unavailable: sensitive detail 必须提供正文")
        if detail is not None:
            record = owner._detail_store.write(
                session_id=snapshot.session_id,
                assembly_id=snapshot.assembly_id,
                detail_kind="assembly_snapshot",
                retention_class="assembly_audit",
                visibility="internal",
                detail=detail,
                required=required_detail,
                sensitive=sensitive_detail,
                checkpoint_ns=checkpoint_ns,
            )
            owner._storage.register_context_plan_detail(record)
        return owner._storage.seal_context_assembly(
            snapshot,
            seal_idempotency_key=seal_idempotency_key,
            seal_input_hash=seal_input_hash,
            detail_ref=record.detail_ref if record else None,
            checkpoint_ns=checkpoint_ns,
            activation_binding=bind_activation,
        )
    except BaseException as error:
        record_failed_seal(
            owner,
            snapshot.session_id,
            snapshot.plan_id,
            seal_idempotency_key,
            checkpoint_ns,
            error,
        )
        if record is not None:
            release_uncommitted_details(
                owner, snapshot.session_id, checkpoint_ns, (record,)
            )
        raise
