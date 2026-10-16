"""已注册 draft 的 Saver seal 协调；随机 identity 只能在重试校验之后分配。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from uuid import uuid4

from langchain_core.messages import BaseMessage

from app.agents.providers.wire_finalize import (
    WIRE_PROJECTOR_ID,
    WIRE_PROJECTOR_VERSION,
    ChatWireProfile,
    ResponsesWireProfile,
    finalize_provider_wire,
)
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.seal_binding import ContextSealPreparation
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.assembly.plans.types import (
    ContextPlanRegistration,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.assembly import (
    seal_snapshot,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.cleanup import (
    record_failed_seal,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    committed_retry,
    require_key,
    require_registered_draft,
    seal_input_hash,
)


@dataclass(frozen=True, slots=True)
class ContextPlanSealResult:
    """本次 seal 的提交结果；投影只随当前调用返回，不进入跨请求缓存。"""

    snapshot: ContextAssemblySnapshot
    projection: tuple[list[BaseMessage], list[dict[str, object]], tuple[str, ...], dict[str, object] | None] | None


def seal_registered_plan(
    owner,
    session_id: str,
    thread_id: str,
    plan: ContextRequestPlan,
    *,
    seal_idempotency_key: str,
    checkpoint_ns: str,
    assembly_options: Mapping[str, object],
    request_only_content: Mapping[str, object] | None,
    request_input_hash: str | None,
    wire_profile: ChatWireProfile | ResponsesWireProfile | None,
    wire_params: Mapping[str, object] | None,
    activation_snapshot: ResourceActivationSnapshotRef | None = None,
    context_preparation: ContextSealPreparation | None = None,
    registered: ContextPlanRegistration | None = None,
) -> ContextPlanSealResult:
    require_key(seal_idempotency_key, field="seal_idempotency_key")
    if (wire_profile is None) != (wire_params is None):
        raise ValueError("source-mismatch: wire profile/params 必须成对提供")
    wire_configuration = None
    if wire_profile is not None:
        if not isinstance(wire_profile, (ChatWireProfile, ResponsesWireProfile)):
            raise TypeError("source-mismatch: wire seal 需要纯值 profile")
        if not isinstance(wire_params, Mapping):
            raise TypeError("source-mismatch: wire seal 需要纯数据 params")
        target_format = "responses" if isinstance(wire_profile, ResponsesWireProfile) else "chat_completions"
        if assembly_options["provider_version"] != wire_profile.identity() or assembly_options["target_format"] != target_format:
            raise ValueError("source-mismatch: wire profile 与 seal 声明不一致")
        if context_preparation is None:
            raise ValueError("source-mismatch: wire seal 缺少封存 prefix 准备")
        owner.require_provider_resource_activation(
            activation_snapshot, session_id=session_id, thread_id=thread_id,
            execution_id=assembly_options["execution_id"], turn_id=assembly_options["turn_id"],
            model_call_id=assembly_options["model_call_id"], checkpoint_ns=checkpoint_ns,
        )
        wire_configuration = {
            "wire_profile": asdict(wire_profile),
            "wire_params": normalize_wire_request(wire_params),
            "projector_id": WIRE_PROJECTOR_ID,
            "projector_version": WIRE_PROJECTOR_VERSION,
        }
        assembly_options = {
            **assembly_options,
            "projector_id": WIRE_PROJECTOR_ID,
            "projector_version": WIRE_PROJECTOR_VERSION,
        }
    with owner._lock:
        registered = require_registered_draft(
            owner, session_id, thread_id, plan, checkpoint_ns,
            registered=registered,
        )
        draft = registered.draft
        # activation binding identity 进入 seal input preimage：重试必须复用同一
        # 冻结 snapshot，不得在同一 seal key 下换成另一个 activation 结果。
        activation_bindings_hash = (
            None
            if activation_snapshot is None
            else f"{activation_snapshot.activation_snapshot_id}|"
            f"{activation_snapshot.bindings_hash}|"
            f"{activation_snapshot.activation_provenance_hash}"
        )
        input_hash = seal_input_hash(
            draft,
            checkpoint_ns,
            assembly_options,
            request_input_hash,
            activation_bindings_hash,
            context_preparation_hash=(
                context_preparation.input_hash()
                if context_preparation is not None else None
            ),
            wire_configuration=wire_configuration,
        )
        retry = committed_retry(
            owner,
            registered,
            session_id=session_id,
            thread_id=thread_id,
            seal_idempotency_key=seal_idempotency_key,
            input_hash=input_hash,
            checkpoint_ns=checkpoint_ns,
            assembly_options=assembly_options,
            request_only_content=request_only_content,
        )
        if retry is not None:
            # 重试不得重新绑定 activation；已提交 assembly 的 activation 事实
            # 必须与本冻结 snapshot 一致，否则显式拒绝而非覆盖。
            if activation_snapshot is not None:
                owner.verify_resource_activation_binding(
                    session_id,
                    assembly_id=retry.assembly_id,
                    activation_snapshot=activation_snapshot,
                    checkpoint_ns=checkpoint_ns,
                )
            return ContextPlanSealResult(
                retry,
                None if wire_profile is None else owner.project_context_plan_for_dispatch(
                    session_id, thread_id, retry.as_sealed_plan(),
                    target_format=retry.target_format, checkpoint_ns=checkpoint_ns,
                ),
            )
        # activation 正文必须在 assemble 之前准备：正文写入失败不能留下已完成
        # assemble 的中间状态；绑定闭包只在 assembly_sealed 事务内使用。
        activation_binding = None
        if activation_snapshot is not None:
            _, activation_binding = owner.prepare_resource_activation_binding(
                activation_snapshot, checkpoint_ns=checkpoint_ns
            )
        assembly_id = f"assembly-{uuid4().hex}"
        projection = None
        try:
            bound, prepared_details, detail_refs = owner._bind_request_detail_refs(
                session_id,
                thread_id,
                checkpoint_ns,
                draft,
                assembly_id=assembly_id,
                omitted_ref_ids=frozenset(assembly_options["omitted_ref_ids"]),
                request_only_content=request_only_content,
            )
            # detail_refs 绑定 included request_only 的真实正文；整体 assembly 审计详情是可选的，可以为空。
            snapshot = owner._composer_for(
                session_id, thread_id, checkpoint_ns
            ).assembly(
                plan=bound,
                assembly_id=assembly_id,
                session_id=session_id,
                thread_id=thread_id,
                request_detail_refs=detail_refs,
                request_input_hash=request_input_hash,
                seal_binding=(
                    context_preparation.bind(assembly_id)
                    if context_preparation is not None else None
                ),
                **assembly_options,
            )
            if wire_profile is not None:
                sealed_plan = snapshot.as_sealed_plan()
                if snapshot.seal_binding is None:
                    raise ValueError("source-mismatch: wire seal 缺少已绑定 prefix")
                item_ids = tuple(
                    entry.ref.ref_id for entry in snapshot.selection
                    if entry.ref.ref_type == "canonical_item" and (
                        entry.included or snapshot.selection_policy == "summary_candidate"
                    )
                )
                items = owner._storage.storage.read_items(
                    session_id, thread_id, item_ids=item_ids, checkpoint_ns=checkpoint_ns,
                )
                bodies = owner._request_content_for_plan(
                    session_id, thread_id, checkpoint_ns, sealed_plan, None,
                    prepared_details=prepared_details,
                )
                projection = owner._project_context_projection(
                    session_id, thread_id, snapshot, items, bodies,
                    include_runtime_notices=True, include_summaries=True,
                    target_format=snapshot.target_format,
                    include_native=isinstance(wire_profile, ResponsesWireProfile),
                )
                messages, tools, _losses, native = projection
                preimage = normalize_wire_request(finalize_provider_wire(
                    messages=messages, tools=tools, native_request=None if native is None else native["request"],
                    profile=wire_profile, params=wire_params,
                ))
                snapshot = replace(
                    snapshot, request_hash_preimage=preimage,
                    request_hash=context_request_hash(
                        sealed_plan, snapshot.provider_version,
                        projector_id=snapshot.projector_id, projector_version=snapshot.projector_version,
                        target_format=snapshot.target_format, wire_request=preimage,
                    ),
                )
        except BaseException as error:
            record_failed_seal(
                owner,
                session_id,
                thread_id,
                draft.plan_id,
                seal_idempotency_key,
                checkpoint_ns,
                error,
            )
            raise
        committed = seal_snapshot(
            owner,
            session_id,
            thread_id,
            snapshot,
            registered=registered,
            seal_idempotency_key=seal_idempotency_key,
            seal_input_hash=input_hash,
            checkpoint_ns=checkpoint_ns,
            detail=None, required_detail=False, sensitive_detail=False,
            activation_binding=activation_binding,
            prepared_details=prepared_details,
        )
        # storage owner 已核对 hash、registry CAS、manifest 和 activation 并原子提交。
        # ack 只同步 CSM 内存，不修改这些 sealed 事实；本次输出不再冷读同一 assembly。
        return ContextPlanSealResult(committed.snapshot, projection)
