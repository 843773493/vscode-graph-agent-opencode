"""正向来源准备、唯一 assembly seal 与封存后确认的 Provider 边界。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from typing import Literal

from langchain_core.messages import BaseMessage

from app.agents.policy.tool_capability import ToolCapabilityPolicy, ToolMetadata
from app.agents.providers.wire_finalize import (
    WIRE_PROJECTOR_ID,
    WIRE_PROJECTOR_VERSION,
    ChatWireProfile,
    ResponsesWireProfile,
    finalize_provider_wire,
)
from app.agents.skill_context import (
    install_frozen_skill_catalog,
)
from app.core.execution_admission_context import require_current_execution_admission
from app.core.failures import mark_public_failure
from app.domain.context_compaction import ContextCapacityError, ModelCapacity
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.compaction import (
    FrozenCompactionRange,
    FrozenSummaryResult,
    compaction_selection_ids,
)
from app.domain.itemized.epoch.prefix_epoch import (
    EpochReason,
    PendingPrefixEpochTransition,
)
from app.domain.itemized.epoch.seal_binding import ContextSealPreparation
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import (
    ContextRef,
    ref_identity,
    selection_ref_identity,
)
from app.domain.itemized.plans.contributions import (
    ContextContribution,
    ContributionResolver,
)
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)
from app.domain.itemized.serde.registry import parse_context_ref
from app.domain.itemized.serialization import normalize_wire_request
from app.services.business.resource_derivations.team import (
    TEAM_FACETS,
    TEAM_RESOURCE_IDS,
    TEAM_SOURCE_IDS,
    team_guidance_content,
)
from app.services.infrastructure.mcp.extension_catalog import (
    MCP_GUIDANCE_SOURCE_ID,
    MCP_TOOL_CATALOG_RESOURCE_ID,
    ExtensionCatalogUnavailableError,
)
from app.services.infrastructure.mcp.frozen_catalog import (
    FrozenToolDefinition,
    mcp_guidance_content,
    restore_frozen_tool_catalog,
)
from app.services.infrastructure.rollout_context.assembly.manifest import (
    _frozen_source_refs,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    _ValidatedPredecessors,
)
from app.services.infrastructure.rollout_context.assembly.plans.types import (
    ContextPlanRegistration,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    ToolProtocolBoundary,
)
from app.services.infrastructure.rollout_context.checkpoint.message_commit.compaction import (
    build_candidate_summary_item,
    read_summary_sources,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_projection import (
    ContextProjectionInput,
    ContextProjectionResult,
    ProviderContextRequest,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.lifecycle import (
    seal_registered_plan,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    require_key,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.agents_md_context import (
    install_frozen_agents_md_chain,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    ContextSourceDelta,
    ContextSourceDescriptor,
    ContextSourceSnapshot,
    PendingContextSourceBatch,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.skill_catalog import (
    skill_catalog_from_activation,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.tool_definitions import (
    DiscoveredToolRef,
    tool_definition_batch,
)
from app.services.infrastructure.rollout_context.runtime.derived_compiler import (
    request_ref,
)
from app.services.infrastructure.rollout_context.runtime.prefix_compiler import (
    compile_prefix_context,
    source_contributions,
)
from app.services.infrastructure.rollout_context.runtime.summary_compiler import (
    compaction_history_items,
)


@dataclass(frozen=True, slots=True)
class PreparedProviderContext:
    """本次冻结组装结果；预算和封存共用，不进入跨请求缓存或持久账本。"""

    session_id: str
    thread_id: str
    turn_id: str | None
    execution_id: str
    checkpoint_ns: str
    model_call_id: str
    provider_version: str
    target_format: str
    creation_key: str
    seal_key: str
    input_hash: str
    plan: ContextRequestPlan
    preparation: ContextSealPreparation
    bodies: Mapping[str, object]
    omitted: tuple[tuple[str, ...], ...]
    validated_predecessors: _ValidatedPredecessors
    activation: ResourceActivationSnapshotRef
    wire_profile: ChatWireProfile | ResponsesWireProfile
    wire_params: Mapping[str, object]
    capacity: ModelCapacity | None
    source_manager: ContextSourceManager | None
    source_batch: PendingContextSourceBatch | None
    request: ProviderContextRequest | None
    simulate_candidate: Callable[[FrozenCompactionRange, FrozenSummaryResult], ProviderContextRequest] | None
    item_sequence: int = 0
    reassess_sources: Callable[[ContextAssemblySnapshot, Sequence[CanonicalItemRecord]], tuple[ProviderContextRequest, tuple[ContextContribution, ...], ToolSetSnapshot]] | None = None
    project_retained_input: Callable[[], ProviderContextRequest] | None = None


class ProviderPreparationConflict(RuntimeError):
    """来源冻结期间事实推进；调用方至多重新冻结一次。"""


def _require_sealed_wire(
    snapshot: ContextAssemblySnapshot, projection: ContextProjectionResult, *,
    wire_profile: ChatWireProfile | ResponsesWireProfile, wire_params: Mapping[str, object],
) -> dict[str, object]:
    """新 seal 与幂等恢复共用最终 wire 校验，不信任后来漂移的 projection。"""
    preimage = normalize_wire_request(finalize_provider_wire(
        messages=projection.messages, tools=projection.tools,
        native_request=None if projection.native is None else projection.native["request"],
        profile=wire_profile, params=wire_params,
    ))
    if canonical_json_bytes(snapshot.request_hash_preimage) != canonical_json_bytes(preimage):
        raise ValueError("source-mismatch: 最终 Provider request 与封存 request hash preimage 不一致")
    return preimage


def _project_prepared_context(
    owner, *, plan: ContextRequestPlan, preparation: ContextSealPreparation,
    items: Sequence[CanonicalItemRecord], bodies: Mapping[str, object],
    turn_id: str | None, execution_id: str, model_call_id: str,
    activation: ResourceActivationSnapshotRef, provider_version: str, target_format: str,
    wire_profile: ChatWireProfile | ResponsesWireProfile, wire_params: Mapping[str, object],
    omitted: tuple[tuple[str, ...], ...] = (),
) -> ProviderContextRequest:
    """沿正式 composer 和 projector 计算完整请求，不注册 draft 或分配正文。"""
    assembly_id = f"preview:{plan.plan_id}"
    details = {selection_ref_identity(ref): DetailRef(plan.session_id, plan.thread_id, assembly_id, f"source-{index}")
               for index, ref in enumerate(plan.refs) if ref.ref_type == "request_only"}
    snapshot = ContextPlanComposer().assembly(
        plan=plan, assembly_id=assembly_id, session_id=plan.session_id, thread_id=plan.thread_id,
        turn_id=turn_id, execution_id=execution_id, model_call_id=model_call_id,
        provider_version=provider_version, target_format=target_format,
        request_detail_refs=details, omitted_ref_identities=omitted,
        seal_binding=preparation.bind(assembly_id),
    )
    by_id = {item.item_id: item for item in items}
    materialized = {ref_identity(ref): by_id[ref.ref_id] for ref in plan.refs
                    if ref.ref_type == "canonical_item" and ref.ref_id in by_id}
    return owner.project_frozen_context_request(
        ContextProjectionInput(snapshot, snapshot.as_sealed_plan(), activation), materialized, bodies,
        wire_profile=wire_profile, wire_params=wire_params,
    )


def _activation_guidance_batch(
    read_payload: Callable[[ResourceActivationSnapshotRef, ResourceProvenanceRef], object],
    activation: ResourceActivationSnapshotRef, previous: ContextAssemblySnapshot | None,
    batch: PendingContextSourceBatch,
) -> PendingContextSourceBatch:
    """来源正文只由本次冻结 activation 派生，不登记第二个 CSM owner。"""
    specs = ((MCP_TOOL_CATALOG_RESOURCE_ID, MCP_GUIDANCE_SOURCE_ID, "mcp_tool_catalog", "MCP 工具指引", mcp_guidance_content),
             *((TEAM_RESOURCE_IDS[facet], TEAM_SOURCE_IDS[facet], "team_state", f"团队{facet}", team_guidance_content)
               for facet in TEAM_FACETS))
    previous_frames = () if previous is None or previous.seal_binding is None else previous.seal_binding.prefix.state.frames
    sources, deltas = list(batch.sources), list(batch.deltas)
    for resource_id, source_id, source_kind, name, read_content in specs:
        if any(source.descriptor.source_id == source_id for source in batch.sources):
            raise ValueError("context-source-owner-conflict: activation guidance 不得登记为 CSM source")
        parents = tuple(binding for binding in activation.bindings if binding.resource_id == resource_id)
        if len(parents) > 1:
            raise ValueError("resource-activation-snapshot-conflict: guidance parent 不唯一")
        previous_revision = next((ref.source_revision for frame in reversed(previous_frames) for ref in reversed(frame.item_refs)
                                  if ref.source_identity == source_id), None)
        if not parents and previous_revision is None:
            continue
        content = read_content(read_payload(activation, parents[0])) if parents else ""
        if previous_revision is None and not content:
            continue
        revision = parents[0].revision if parents else sha256_jcs({"content": ""})
        sources.append(ContextSourceSnapshot(
            descriptor=ContextSourceDescriptor(source_id=source_id, source_kind=source_kind, name=name,
                                               description="本次冻结的来源正文", internal_locator=resource_id,
                                               root_placement="tail_only"),
            revision=revision, content=content, applied_revision=previous_revision, tracked=False,
        ))
        if revision != previous_revision:
            deltas.append(ContextSourceDelta(
                source_id=source_id, source_kind=source_kind, source_name=name, revision=revision,
                previous_revision=previous_revision, content=content, content_hash=sha256_jcs(content),
                kind="activation" if previous_revision is None else "delta",
            ))
    return PendingContextSourceBatch(deltas=tuple(deltas), sources=tuple(sources))


def prepared_context_plan_id(creation_key: str) -> str:
    """创建与只读恢复共用唯一 plan identity 算法。"""

    require_key(creation_key, field="plan_creation_idempotency_key")
    return "plan-prepared:" + sha256_jcs({"creation_key": creation_key})


def freeze_registered_context(
    owner, session_id: str, thread_id: str, *,
    turn_id: str | None, execution_id: str,
    plan_creation_idempotency_key: str, seal_idempotency_key: str,
    request_messages: Sequence[BaseMessage] = (),
    context_source_manager: ContextSourceManager | None = None,
    tool_set_supplier: Callable[[], ToolSetSnapshot] | None = None,
    extension_policy_resolver: Callable[[Sequence[ToolMetadata]], Mapping[str, ToolCapabilityPolicy]] | None = None,
    provider_version: str, wire_profile: ChatWireProfile | ResponsesWireProfile,
    wire_params: Mapping[str, object],
    target_format: str, checkpoint_ns: str,
    model_call_id: str,
    activation_snapshot: ResourceActivationSnapshotRef,
    frozen_plan: ContextRequestPlan | None = None,
    context_preparation: ContextSealPreparation | None = None,
    request_only_content: Mapping[str, object] | None = None,
    sealed_context: ContextPlanRegistration | None = None,
    allow_provider_reprojection: bool = False,
    source_message_grouping: Literal["aggregate", "separate"] = "aggregate",
    model_capacity: ModelCapacity | None = None,
) -> PreparedProviderContext | dict[str, object]:
    require_key(plan_creation_idempotency_key, field="plan_creation_idempotency_key")
    require_key(seal_idempotency_key, field="seal_idempotency_key")
    if not isinstance(wire_params, Mapping):
        raise TypeError("provider context 需要纯数据 wire_params")
    if not isinstance(wire_profile, (ChatWireProfile, ResponsesWireProfile)):
        raise TypeError("provider context 需要纯值 wire_profile")
    profile_format = "responses" if isinstance(wire_profile, ResponsesWireProfile) else "chat_completions"
    if provider_version != wire_profile.identity() or target_format != profile_format:
        raise ValueError("source-mismatch: provider wire profile 与声明不一致")
    admission = require_current_execution_admission(langgraph_thread_id=thread_id)
    if (admission.session_id, admission.thread_id, admission.execution_id, admission.turn_id) != (
        session_id, thread_id, execution_id, turn_id,
    ):
        raise RuntimeError("provider context 与当前 execution admission 不一致")
    # 冻结资源读取已通过 rollout 快照校验格式、schema 和归属，无须再打开快照探测能力。
    owner.require_provider_resource_activation(
        activation_snapshot, session_id=session_id, thread_id=thread_id,
        execution_id=execution_id, turn_id=turn_id, model_call_id=model_call_id,
        checkpoint_ns=checkpoint_ns,
    )
    if target_format not in {"chat_completions", "responses"}:
        raise ValueError("不支持的 provider context target_format: " + target_format)
    derived = frozen_plan is not None
    if derived:
        if not isinstance(context_preparation, ContextSealPreparation) or request_only_content is None:
            raise TypeError("派生请求必须同时冻结 plan、preparation 与 request-only 正文")
        if (frozen_plan.session_id, frozen_plan.thread_id, frozen_plan.selection_policy, frozen_plan.plan_state) != (
            session_id, thread_id, "derived_request", "unsealed",
        ) or admission.execution_origin != "internal" or turn_id is not None:
            raise ValueError("source-mismatch: 派生请求归属或 selection 非法")
        if (context_preparation.previous_assembly_id is not None or context_preparation.source_revisions
                or context_preparation.transition is not None or context_preparation.notice_acceptance is not None
):
            raise ValueError("source-mismatch: 派生请求不得消费主上下文来源、通知或epoch切换")
    elif context_preparation is not None or request_only_content is not None:
        raise ValueError("冻结请求必须提供 typed plan")
    elif not isinstance(context_source_manager, ContextSourceManager) or tool_set_supplier is None:
        raise TypeError("Graph provider context 需要唯一 ContextSourceManager 与 ToolSet producer")
    # prepare 冻结身份（seal_input_hash 的一项）：只进注册表与 seal 提交记录，不进 request_hash。
    input_hash = sha256_jcs({
        "schema": "provider-prepare-input:v3",
        "request_messages": [message.model_dump(mode="json") for message in request_messages],
        "wire_profile": asdict(wire_profile),
        "wire_params": normalize_wire_request(wire_params),
        "projector_id": WIRE_PROJECTOR_ID,
        "projector_version": WIRE_PROJECTOR_VERSION,
        "frozen_plan": frozen_plan.plan_hash() if derived else None,
        "context_preparation": context_preparation.input_hash() if derived else None,
    })
    plan_id = frozen_plan.plan_id if derived else prepared_context_plan_id(plan_creation_idempotency_key)
    with owner._lock:
        registered = sealed_context if sealed_context is not None else owner.find_sealed_context_for_provider_creation(
            session_id, thread_id, plan_creation_idempotency_key=plan_creation_idempotency_key,
            checkpoint_ns=checkpoint_ns, plan_id=plan_id,
        )
        reused_sealed = registered is not None
        if registered is not None:
            if (registered.session_id, registered.thread_id, registered.draft.plan_id,
                registered.draft.plan_creation_idempotency_key, registered.plan_state) != (
                session_id, thread_id, plan_id, plan_creation_idempotency_key, "sealed",
            ):
                raise ValueError("source-mismatch: sealed provider registration owner/key 不一致")
            if registered.seal_idempotency_key != seal_idempotency_key:
                raise ValueError("assembly-idempotency-conflict: 已绑定另一个 seal key")
            snapshot = registered.sealed_snapshot
            if snapshot is None:
                raise ValueError("source-mismatch: sealed provider registration 缺少 snapshot")
            # prepare 输入身份由 seal 提交记录（snapshot.request_input_hash）承载；
            # request_hash 只绑定真实 wire，不再往 preimage 里塞 prepare 包装。
            if snapshot.request_input_hash != input_hash or (
                snapshot.turn_id != turn_id or snapshot.execution_id != execution_id
                or snapshot.provider_version != provider_version or snapshot.target_format != target_format
                or snapshot.projector_id != WIRE_PROJECTOR_ID
                or snapshot.projector_version != WIRE_PROJECTOR_VERSION
                or snapshot.seal_binding is None
            ):
                raise ValueError("assembly-idempotency-conflict: sealed provider request 身份不一致")
            owner.verify_resource_activation_binding(
                session_id, assembly_id=snapshot.assembly_id,
                activation_snapshot=activation_snapshot, checkpoint_ns=checkpoint_ns,
            )
            if snapshot.model_call_id != model_call_id:
                raise ValueError("assembly-idempotency-conflict: sealed model-call identity 不一致")
            projection = owner.project_context_plan_for_dispatch(
                session_id, thread_id, snapshot.as_sealed_plan(),
                target_format=target_format, checkpoint_ns=checkpoint_ns,
                wire_profile=wire_profile,
            )
        else:
            # 成功 SQL 行仅沿本次 prepare 传给 seal writer 重读，不跨请求保存。
            validated_predecessors: _ValidatedPredecessors = {}
            if derived:
                plan, preparation, bodies = frozen_plan, context_preparation, dict(request_only_content)
                # merge coverage 只作为冷验证 backing；新原文仍实际进入 wire。
                covered = {
                    selection_ref_identity(ref)
                    for body in bodies.values() if isinstance(body, Mapping) and "result_ref" in body
                    for ref in (parse_context_ref(value) for value in (*body["coverage"], *body["output_refs"]))
                }
                omitted = tuple(covered)
            else:
                # Registry 等待期间 canonical 可能推进；封存前复用唯一边界端口重新检查。
                conflict, previous = owner.prepare_model_call_boundary(
                    session_id, thread_id, turn_id=turn_id, execution_id=execution_id,
                    messages=request_messages,
                    checkpoint_ns=checkpoint_ns,
                    validated_predecessors=validated_predecessors,
                )
                if conflict is not None:
                    raise conflict
                # 只有新请求读取 desired；已 sealed/in-flight attempt 完全复用旧 binding。
                tool_set = tool_set_supplier()
                if not isinstance(tool_set, ToolSetSnapshot):
                    raise TypeError("Tool producer 必须返回正向 ToolSetSnapshot")
                # context-control gate 内先接受权威 tracking/CAS，再消费本次冻结来源。
                context_source_manager.refresh_control_states()
                with ExitStack() as skill_reads:
                    skill_read_snapshot = None

                    def read_skill_payload(frozen, binding):
                        nonlocal skill_read_snapshot
                        # 同次目录恢复共用事务，逐正文仍核归属和摘要；无 Skill 时不打开快照。
                        if skill_read_snapshot is None:
                            skill_read_snapshot = skill_reads.enter_context(owner._context_reader.open_snapshot(
                                session_id, thread_id, checkpoint_ns,
                            ))
                        return owner.read_resource_activation_payload(
                            frozen, binding, checkpoint_ns=checkpoint_ns,
                            storage_owner=(session_id, thread_id), read_snapshot=skill_read_snapshot,
                        )

                    frozen_skills = skill_catalog_from_activation(activation_snapshot, read_skill_payload)
                    pending_catalog = None
                    if frozen_skills is not None and previous is not None and context_source_manager.pending_skill_selection_ids():
                        # 待封存的显式选择来自上一已封存调用，新目录不能覆盖原选择。
                        # 没有待选择正文时不恢复旧目录，避免每次新请求重复读取全部旧来源。
                        original = owner.load_sealed_resource_activation(
                            session_id, thread_id=thread_id, assembly_id=previous.assembly_id,
                            plan_hash=previous.plan_hash, request_hash=previous.request_hash,
                            checkpoint_ns=checkpoint_ns,
                        )
                        pending_catalog = skill_catalog_from_activation(original, read_skill_payload)
                # 关闭正文读取快照后才安装 CSM 状态，避免持读锁进入控制状态写事务。
                if frozen_skills is not None:
                    install_frozen_skill_catalog(
                        context_source_manager, frozen_skills,
                        include_instructions="skill_load" in tool_set.tool_names,
                        pending_catalog=pending_catalog,
                    )
                rebuild = owner.load_context_source_rebuild_state(
                    ContextSourceOwnerKey(session_id, thread_id), checkpoint_ns=checkpoint_ns,
                )
                context_source_manager.refresh_context_rebuild_state(rebuild)
                owner.install_runtime_context_sources(session_id, thread_id, context_source_manager)

                def read_agents_committed_activation() -> ResourceActivationSnapshotRef:
                    source_assembly = rebuild.source_assembly if rebuild is not None else previous
                    if source_assembly is None:
                        raise RuntimeError("resource-activation-unavailable: 已 applied AGENTS chain 缺少封存前驱")
                    return owner.load_sealed_resource_activation(
                        session_id, thread_id=thread_id, assembly_id=source_assembly.assembly_id,
                        plan_hash=source_assembly.plan_hash, request_hash=source_assembly.request_hash,
                        checkpoint_ns=checkpoint_ns,
                    )

                install_frozen_agents_md_chain(
                    context_source_manager, activation_snapshot,
                    lambda frozen, binding: owner.read_resource_activation_payload(
                        frozen, binding, checkpoint_ns=checkpoint_ns,
                        storage_owner=(session_id, thread_id),
                    ),
                    read_agents_committed_activation,
                )
                frozen = context_source_manager.prepare_sources(rebuild)
                allowed = {
                    source.descriptor.source_id for source in frozen.sources
                    if source.descriptor.policy_key is None or source.descriptor.policy_key in tool_set.policy_keys
                }
                batch = PendingContextSourceBatch(
                    deltas=tuple(delta for delta in frozen.deltas if delta.source_id in allowed),
                    sources=tuple(source for source in frozen.sources if source.descriptor.source_id in allowed),
                )
                csm_batch = batch
                if admission.execution_origin == "internal":
                    seed_source = owner.internal_seed_source(session_id, thread_id, execution_id)
                    if seed_source is not None:
                        batch = PendingContextSourceBatch(deltas=batch.deltas, sources=(*batch.sources, seed_source))
                with owner._context_reader.open_snapshot(session_id, thread_id, checkpoint_ns) as read_snapshot:
                    assemblies = owner._storage.assemblies
                    latest_id = assemblies._latest_context_assembly_id_connection(
                        read_snapshot.connection, session_id, thread_id, checkpoint_ns=checkpoint_ns,
                    )
                    # 不可变 sealed 前驱同 ID 时复用本次已校验值；资格或身份变化
                    # 仍在当前 snapshot 完整读取，writer 再独立校验并执行 CAS。
                    if latest_id != (None if previous is None else previous.assembly_id):
                        previous = None if latest_id is None else assemblies._read_context_assembly_connection(
                            read_snapshot.connection, session_id, thread_id,
                            assembly_id=latest_id, checkpoint_ns=checkpoint_ns,
                            validated_predecessors=validated_predecessors,
                        )[0]
                    initial_assembly = previous is not None and (
                        previous.session_id, previous.thread_id
                    ) != (session_id, thread_id)
                    items = owner._storage.checkpoints.committed_context_items(read_snapshot, predecessor=previous)
                    retained_items = owner._storage.checkpoints.precompaction_context_items(read_snapshot)
                    canonical_manifest = read_snapshot.manifest
                    item_sequence = read_snapshot.connection.execute(
                        "SELECT last_item_sequence FROM database_meta WHERE singleton_id=1",
                    ).fetchone()[0]
                    # 同批 guidance 与工具定义借用当前快照；关闭后才登记来源，避免逐正文重开连接。
                    def read_payload(frozen, binding):
                        return owner.read_resource_activation_payload(
                            frozen, binding, storage_owner=(session_id, thread_id), checkpoint_ns=checkpoint_ns,
                            read_snapshot=read_snapshot,
                        )

                    # 导入 assembly 是目标本地历史，不是目标已 applied 的请求前缀。
                    # 首次请求只携带实际复制并封存的来源，不回读源会话或未复制正文。
                    retained = rebuild.source_assembly if rebuild is not None else previous if initial_assembly else None
                    retained_refs = rebuild.retained_source_refs if rebuild is not None else frozenset(
                        (ref_id, identity[1]) for ref_id, identity in _frozen_source_refs(retained)[0].items()
                    ) if retained is not None else frozenset()
                    batch = _activation_guidance_batch(
                        read_payload, activation_snapshot, previous, batch,
                    )
                    def resolve_discoveries(
                        refs: Sequence[DiscoveredToolRef],
                    ) -> dict[DiscoveredToolRef, FrozenToolDefinition]:
                        # 先汇总实际消费的定义，再逐原调用核归属；同一 activation 只读取这些正文。
                        requested: dict[str, set[str]] = {}
                        for ref in refs:
                            requested.setdefault(ref.activation_snapshot_id, set()).add(ref.tool_id)
                        activations: dict[str, ResourceActivationSnapshotRef] = {}
                        for tool_call_id, original_call_id, activation_id in dict.fromkeys(
                            (ref.tool_call_id, ref.model_call_id, ref.activation_snapshot_id) for ref in refs
                        ):
                            original, _ = owner.load_tool_call_resource_activation(
                                session_id, thread_id=thread_id, tool_call_id=tool_call_id,
                                model_call_id=original_call_id, checkpoint_ns=checkpoint_ns,
                            )
                            if original.activation_snapshot_id != activation_id:
                                raise ExtensionCatalogUnavailableError("发现结果与原工具调用 activation 不一致")
                            activations[activation_id] = original
                        definitions = {
                            activation_id: {value.metadata.tool_id: value for value in restore_frozen_tool_catalog(
                                original, read_payload, tool_ids=frozenset(requested[activation_id]),
                            ).definitions}
                            for activation_id, original in activations.items()
                        }
                        resolved = {}
                        for ref in refs:
                            definition = definitions[ref.activation_snapshot_id].get(ref.tool_id)
                            if (definition is None or definition.provenance.resource_id != ref.resource_id
                                    or definition.provenance.revision != ref.revision):
                                raise ExtensionCatalogUnavailableError("发现结果引用的原冻结定义缺失或身份冲突")
                            resolved[ref] = definition
                        return resolved

                    guidance = next((source.content for source in batch.sources
                                     if source.descriptor.source_id == MCP_GUIDANCE_SOURCE_ID), None)
                    automatic = frozenset(entry["tool_name"] for entry in json.loads(guidance)["automatic_definitions"]) if guidance else frozenset()
                    batch = tool_definition_batch(
                        batch, catalog_supplier=lambda tool_ids: restore_frozen_tool_catalog(
                            activation_snapshot, read_payload, tool_ids=tool_ids,
                        ), current_items=items, retained_items=retained_items,
                        previous=previous, retained_assembly=retained if retained is not None else previous,
                        retained_source_refs=retained_refs if retained is not None else None,
                        resolve_discoveries=resolve_discoveries, policies_for=extension_policy_resolver,
                        automatic_tool_ids=automatic, restoring=rebuild is not None or initial_assembly,
                    )
                bodies = {}
                for contribution in source_contributions(batch):
                    owner.register_context_contribution(
                        session_id, thread_id, contribution, checkpoint_ns=checkpoint_ns,
                        request_content=contribution.body,
                    )
                    bodies[contribution.contribution_id] = contribution.body
                with owner._context_reader.open_snapshot(session_id, thread_id, checkpoint_ns) as read_snapshot:
                    if read_snapshot.manifest != canonical_manifest:
                        raise ProviderPreparationConflict("context-prepare-state-conflict: 来源登记期间 canonical/view 已推进，请重新准备")
                    notice_acceptance, _notice_selection = owner._storage.executions.runtime_notice_acceptance(
                        admission, connection=read_snapshot.connection,
                    )
                    excluded_notices = owner._storage.executions.runtime_notice_excluded_item_ids(
                        admission, connection=read_snapshot.connection,
                    )
                    full_plan = owner._compose_committed_context_plan(
                        read_snapshot, plan_id=plan_id, tool_set=tool_set, committed_items=items,
                        predecessor=previous,
                    )
                    sequences = owner._storage.checkpoints.selected_context_message_sequences(
                        read_snapshot, items=items,
                    )
                    ToolProtocolBoundary(read_snapshot.connection, sequences).validate(len(sequences))
                overlay_bodies = {}
                contribution_resolver = ContributionResolver(full_plan.contributions)
                for ref in full_plan.refs:
                    if ref.base_delta_role not in {"base", "delta"} or ref.availability != "available":
                        continue
                    contribution = contribution_resolver.resolve(ref)
                    body = owner._draft_request_body(
                        session_id, thread_id, checkpoint_ns, full_plan, ref,
                        contribution=contribution,
                    )
                    overlay_bodies[ref.ref_id] = body
                    bodies[contribution.contribution_id] = body
                previous_bodies = {} if previous is None else owner._request_content_for_plan(
                    session_id, thread_id, checkpoint_ns, previous.as_sealed_plan(), None,
                )
                bodies.update(previous_bodies)
                retained_bodies = previous_bodies
                if retained is not None and retained_refs and retained is not previous:
                    retained_bodies = owner._request_content_for_plan(
                        session_id, thread_id, checkpoint_ns, retained.as_sealed_plan(), None,
                    )
                    bodies.update(retained_bodies)
                compile_plan = full_plan
                transition = rebuild.transition if rebuild is not None else None
                if notice_acceptance is not None:
                    applied_ids = set() if previous is None or previous.seal_binding is None else {
                        frame.item_refs[0].ref_id for frame in previous.seal_binding.prefix.state.frames
                        if frame.item_refs[0].ref_type == "canonical_item"
                    }
                    if transition is not None:
                        transition = replace(transition, notice_acceptance=notice_acceptance)
                    elif applied_ids & excluded_notices:
                        transition = PendingPrefixEpochTransition(
                            transition_id=f"epoch-notice:{notice_acceptance.acceptance_commit_id}",
                            reason=EpochReason.RUNTIME_NOTICE_CONSUMED, turn_id=turn_id,
                            notice_acceptance=notice_acceptance,
                        )
                plan, preparation = compile_prefix_context(
                    compile_plan, items, batch, tool_set, overlay_bodies=overlay_bodies,
                    source_message_grouping=source_message_grouping,
                    previous=previous,
                    initial_assembly=initial_assembly,
                    transition=transition,
                    retained_source_assembly=retained,
                    retained_source_refs=retained_refs,
                    retained_source_bodies=retained_bodies,
                    provider_profile=f"{target_format}:{provider_version}", turn_id=turn_id,
                    notice_acceptance=notice_acceptance,
                    allow_provider_reprojection=allow_provider_reprojection,
                    deselected_source_ids=context_source_manager.deselected_skill_source_ids(),
                )
                omitted = ()
            if derived:
                # 派生请求沿显式块计划封存，不触发主会话自动压缩。
                materialized = owner._storage.storage.read_ref_items(
                    session_id, thread_id, refs=tuple(ref for ref in plan.refs if ref.ref_type == "canonical_item"),
                    checkpoint_ns=checkpoint_ns,
                )
                request = _project_prepared_context(
                    owner, plan=plan, preparation=preparation, items=tuple(materialized.values()), bodies=bodies,
                    turn_id=turn_id, execution_id=execution_id, model_call_id=model_call_id,
                    activation=activation_snapshot, provider_version=provider_version, target_format=target_format,
                    wire_profile=wire_profile, wire_params=wire_params, omitted=omitted,
                )
                return PreparedProviderContext(
                    session_id, thread_id, turn_id, execution_id, checkpoint_ns, model_call_id,
                    provider_version, target_format, plan_creation_idempotency_key, seal_idempotency_key,
                    input_hash, plan, preparation, bodies, omitted, validated_predecessors,
                    activation_snapshot, wire_profile, wire_params, model_capacity, None, None, request, None,
                )

            def project(candidate_plan, candidate_preparation, candidate_items):
                return _project_prepared_context(
                    owner, plan=candidate_plan, preparation=candidate_preparation, items=candidate_items,
                    bodies=bodies, turn_id=turn_id, execution_id=execution_id, model_call_id=model_call_id,
                    activation=activation_snapshot, provider_version=provider_version, target_format=target_format,
                    wire_profile=wire_profile, wire_params=wire_params,
                )

            canonical = tuple(ref for ref in compile_plan.refs if ref.ref_type == "canonical_item")
            by_id = {item.item_id: item for item in items}
            ordered = tuple(by_id[ref.ref_id] for ref in canonical)

            def project_replacement(coverage, summary, identity):
                start = canonical.index(coverage[0])
                if canonical[start:start + len(coverage)] != coverage:
                    raise ValueError("compaction-coverage-mismatch: 模拟范围与冻结候选不一致")
                replacement = (() if summary is None else
                               (ContextRef.canonical_item(summary, session_id=session_id, thread_id=thread_id),))
                candidate_items = (*ordered[:start], *((summary,) if summary is not None else ()), *ordered[start + len(coverage):])
                covered = {ref_identity(ref) for ref in coverage}
                replaced = []
                for ref in compile_plan.refs:
                    if ref_identity(ref) == ref_identity(coverage[0]):
                        replaced.extend(replacement)
                    if ref_identity(ref) not in covered:
                        replaced.append(ref)
                candidate_plan, candidate_preparation = compile_prefix_context(
                    replace(compile_plan, refs=tuple(replaced)), candidate_items, batch, tool_set,
                    overlay_bodies=overlay_bodies, source_message_grouping=source_message_grouping,
                    previous=previous, initial_assembly=initial_assembly,
                    transition=PendingPrefixEpochTransition(
                        transition_id=f"preview-compaction:{identity}", reason=EpochReason.COMPACTION,
                        turn_id=turn_id, notice_acceptance=notice_acceptance,
                    ),
                    retained_source_assembly=previous,
                    retained_source_refs=frozenset((ref_id, identity[1]) for ref_id, identity
                                                  in _frozen_source_refs(previous)[0].items()) if previous is not None else frozenset(),
                    retained_source_bodies=previous_bodies,
                    provider_profile=f"{target_format}:{provider_version}", turn_id=turn_id,
                    notice_acceptance=notice_acceptance,
                    allow_provider_reprojection=allow_provider_reprojection,
                    deselected_source_ids=context_source_manager.deselected_skill_source_ids(),
                )
                return project(candidate_plan, candidate_preparation, candidate_items)

            def simulate_candidate(frozen, result):
                replacing = compaction_selection_ids(frozen, tuple(ref.ref_id for ref in canonical))
                if replacing is None or result.coverage != frozen.refs:
                    raise ValueError("compaction-coverage-mismatch: 模拟范围与冻结候选不一致")
                replacing_ids = set(replacing)
                coverage = tuple(ref for ref in canonical if ref.ref_id in replacing_ids)
                summary_sources = {}
                if any(ref.semantic_kind == "compaction_summary" for ref in frozen.refs):
                    with owner._storage.storage.read_connection(session_id, thread_id, checkpoint_ns) as connection:
                        summary_sources = read_summary_sources(connection, frozen)
                summary = build_candidate_summary_item(
                    frozen, result, item_sequence=max((item.item_sequence for item in items), default=0) + 1,
                    checkpoint_id=f"preview:{frozen.candidate_id}", execution_id=execution_id,
                    turn_id=turn_id, timestamp="1970-01-01T00:00:00Z",
                    summary_sources=summary_sources,
                )
                return project_replacement(coverage, summary, frozen.candidate_id)

            def project_retained_input():
                historical = compaction_history_items(ordered)
                if not historical:
                    return project(plan, preparation, items)
                return project_replacement(canonical[:len(historical)], None, "retained-input")

            def reassess_sources(sealed_snapshot, appended_items):
                # 仅来源变化时重投影；canonical 正文沿用实际冻结值及索引增量，绝不重扫历史。
                current = context_source_manager.read_sources()
                csm_ids = {source.descriptor.source_id for source in csm_batch.sources}
                effective_sources = (*current.sources, *(source for source in batch.sources
                                                        if source.descriptor.source_id not in csm_ids))
                allowed_sources = tuple(source for source in effective_sources
                    if source.descriptor.policy_key is None or source.descriptor.policy_key in tool_set.policy_keys)
                allowed_ids = {source.descriptor.source_id for source in allowed_sources}
                current_batch = PendingContextSourceBatch(
                    deltas=tuple(delta for delta in current.deltas if delta.source_id in allowed_ids),
                    sources=allowed_sources,
                )
                additions = source_contributions(current_batch)
                current_bodies = {**bodies, **{source.contribution_id: source.body for source in additions}}
                base = sealed_snapshot.as_sealed_plan()
                refs = (*tuple(request_ref(base, source) for source in additions),
                        *tuple(ContextRef.canonical_item(item, session_id=session_id, thread_id=thread_id)
                               for item in appended_items))
                current_items = tuple({item.item_id: item for item in (*items, *appended_items)}.values())
                preview_id = f"preview:{base.plan_id}"
                details = {selection_ref_identity(ref): DetailRef(session_id, thread_id, preview_id, f"source-{index}")
                           for index, ref in enumerate((*base.refs, *refs)) if ref.ref_type == "request_only"}
                # 纯 composer 只分配本次预览的 assembly ordinal；不补造 registry ordinal 或持久化 receipt。
                current_plan = ContextPlanComposer().assembly(
                    plan=base, assembly_id=preview_id, session_id=session_id, thread_id=thread_id,
                    turn_id=turn_id, execution_id=execution_id, model_call_id=model_call_id,
                    provider_version=provider_version, target_format=target_format,
                    request_detail_refs=details, additional_refs=refs, additional_contributions=additions,
                ).as_sealed_plan()
                projected_plan, projected_preparation = compile_prefix_context(
                    current_plan, current_items, current_batch, tool_set,
                    overlay_bodies=overlay_bodies, source_message_grouping=source_message_grouping,
                    previous=sealed_snapshot, provider_profile=f"{target_format}:{provider_version}",
                    turn_id=turn_id, notice_acceptance=notice_acceptance,
                    deselected_source_ids=context_source_manager.deselected_skill_source_ids(),
                )
                request = _project_prepared_context(
                    owner, plan=projected_plan, preparation=projected_preparation, items=current_items,
                    bodies=current_bodies, turn_id=turn_id, execution_id=execution_id, model_call_id=model_call_id,
                    activation=activation_snapshot, provider_version=provider_version, target_format=target_format,
                    wire_profile=wire_profile, wire_params=wire_params,
                )
                return request, source_contributions(PendingContextSourceBatch(deltas=(), sources=allowed_sources)), tool_set

            return PreparedProviderContext(
                session_id, thread_id, turn_id, execution_id, checkpoint_ns, model_call_id,
                provider_version, target_format, plan_creation_idempotency_key, seal_idempotency_key,
                input_hash, plan, preparation, bodies, omitted, validated_predecessors,
                activation_snapshot, wire_profile, wire_params, model_capacity, context_source_manager, csm_batch,
                project(plan, preparation, items), simulate_candidate, item_sequence, reassess_sources, project_retained_input,
            )
        plan = projection.plan
        preimage = _require_sealed_wire(snapshot, projection, wire_profile=wire_profile, wire_params=wire_params)
        if reused_sealed and model_capacity is not None and model_capacity.capacity_known:
            budget = ProviderContextRequest(projection, preimage, tuple(projection.messages)).budget(model_capacity)
            if budget.exceeds_capacity:
                raise mark_public_failure(ContextCapacityError(budget))
        pending = {
            "assembly_id": snapshot.assembly_id, "execution_id": snapshot.execution_id,
            "snapshot": snapshot, "plan": plan, "messages": projection.messages,
            "tools": projection.tools, "losses": projection.losses,
            "resource_refs": projection.resource_refs,
        }
        if projection.native is not None:
            pending["native_projection"] = projection.native
        return pending


__all__ = ["PreparedProviderContext", "ProviderPreparationConflict", "freeze_registered_context",
           "prepared_context_plan_id", "seal_prepared_context"]


def seal_prepared_context(owner, prepared: PreparedProviderContext) -> dict[str, object]:
    """只提交上一步冻结的计划；原 writer 仍核 CAS、来源与容量。"""
    with owner._lock:
        registered = owner.create_context_plan(
            prepared.session_id, replace(prepared.plan, plan_creation_idempotency_key=prepared.creation_key),
            checkpoint_ns=prepared.checkpoint_ns,
        )
        sealed = seal_registered_plan(
            owner, prepared.session_id, prepared.thread_id, registered.draft, registered=registered,
            validated_predecessors=prepared.validated_predecessors,
            seal_idempotency_key=prepared.seal_key,
            assembly_options={
                "turn_id": prepared.turn_id, "execution_id": prepared.execution_id,
                "provider_version": prepared.provider_version, "model_call_id": prepared.model_call_id,
                "target_format": prepared.target_format, "loss": (),
                "omitted_ref_identities": frozenset(prepared.omitted),
            },
            wire_profile=prepared.wire_profile, wire_params=prepared.wire_params, model_capacity=prepared.capacity,
            request_input_hash=prepared.input_hash, request_only_content=prepared.bodies,
            context_preparation=prepared.preparation, activation_snapshot=prepared.activation,
            checkpoint_ns=prepared.checkpoint_ns,
        )
        snapshot, projection = sealed.snapshot, sealed.projection
        if projection is None:
            raise RuntimeError("source-mismatch: Provider seal 未返回本次 wire projection")
        _require_sealed_wire(snapshot, projection, wire_profile=prepared.wire_profile, wire_params=prepared.wire_params)
        if prepared.source_manager is not None:
            prepared.source_manager.acknowledge_sealed_pending(prepared.source_batch, snapshot.seal_binding)
        result = {"assembly_id": snapshot.assembly_id, "execution_id": snapshot.execution_id,
                  "snapshot": snapshot, "plan": projection.plan, "messages": projection.messages,
                  "tools": projection.tools, "losses": projection.losses, "resource_refs": projection.resource_refs}
        if projection.native is not None:
            result["native_projection"] = projection.native
        return result
