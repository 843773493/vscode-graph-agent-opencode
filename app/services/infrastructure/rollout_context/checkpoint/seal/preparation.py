"""正向来源准备、唯一 assembly seal 与封存后确认的 Provider 边界。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from functools import cache

from langchain_core.messages import BaseMessage

from app.agents.policy.tool_capability import ToolCapabilityPolicy, ToolMetadata
from app.agents.providers.wire_finalize import (
    WIRE_PROJECTOR_ID,
    WIRE_PROJECTOR_VERSION,
    ChatWireProfile,
    ResponsesWireProfile,
    finalize_provider_wire,
)
from app.agents.skill_runtime import install_frozen_skill_catalog
from app.core.execution_admission_context import require_current_execution_admission
from app.core.model_request_context import SummaryCandidateScope
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.prefix_epoch import (
    EpochReason,
    PendingPrefixEpochTransition,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.refs import ContextRef, selection_ref_identity
from app.domain.itemized.plans.contributions import resolve_contribution_for_ref
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.domain.itemized.serialization import normalize_wire_request
from app.services.business.resource_derivations.team import (
    TEAM_FACETS,
    TEAM_RESOURCE_IDS,
    TEAM_SOURCE_IDS,
    team_guidance_content,
)
from app.services.infrastructure.mcp.catalog_activation import (
    MCP_GUIDANCE_SOURCE_ID,
    MCP_TOOL_CATALOG_RESOURCE_ID,
    mcp_guidance_content,
)
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogUnavailableError,
)
from app.services.infrastructure.mcp.tool_definitions import (
    FrozenToolDefinition,
    tool_catalog_from_activation,
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
    validate_tool_protocol_closure,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.lifecycle import (
    seal_registered_plan,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    require_key,
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
from app.services.infrastructure.rollout_context.runtime.context_sources.workspace_agents import (
    install_frozen_workspace_agents,
)
from app.services.infrastructure.rollout_context.runtime.prefix_compiler import (
    compile_prefix_context,
    source_contributions,
)
from app.services.infrastructure.rollout_context.runtime.summary_compiler import (
    compile_summary_candidate,
)


def _activation_guidance_batch(
    owner, activation: ResourceActivationSnapshotRef, previous: ContextAssemblySnapshot | None,
    batch: PendingContextSourceBatch, *, checkpoint_ns: str,
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
        previous_revision = next((frame.item_ref.source_revision for frame in reversed(previous_frames)
                                  if frame.item_ref.source_identity == source_id), None)
        if not parents and previous_revision is None:
            continue
        content = read_content(owner.read_resource_activation_payload(
            activation, parents[0], checkpoint_ns=checkpoint_ns,
            storage_owner=(activation.owner_session_id, activation.owner_thread_id),
        )) if parents else ""
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


def prepare_registered_context(
    owner, session_id: str, thread_id: str, *,
    turn_id: str | None, execution_id: str,
    plan_creation_idempotency_key: str, seal_idempotency_key: str,
    request_messages: Sequence[BaseMessage],
    context_source_manager: ContextSourceManager,
    tool_set_supplier: Callable[[], ToolSetSnapshot],
    extension_policy_resolver: Callable[[Sequence[ToolMetadata]], Mapping[str, ToolCapabilityPolicy]] | None = None,
    provider_version: str, wire_profile: ChatWireProfile | ResponsesWireProfile,
    wire_params: Mapping[str, object],
    target_format: str, checkpoint_ns: str,
    model_call_id: str,
    activation_snapshot: ResourceActivationSnapshotRef,
    summary_candidate: SummaryCandidateScope | None = None,
    sealed_context: ContextPlanRegistration | None = None,
    allow_provider_reprojection: bool = False,
) -> dict[str, object]:
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
    owner.require_provider_resource_activation(
        activation_snapshot, session_id=session_id, thread_id=thread_id,
        execution_id=execution_id, turn_id=turn_id, model_call_id=model_call_id,
        checkpoint_ns=checkpoint_ns,
    )
    if target_format not in {"chat_completions", "responses"}:
        raise ValueError("不支持的 provider context target_format: " + target_format)
    if not owner.supports_itemized_context(session_id, thread_id, checkpoint_ns=checkpoint_ns):
        raise RuntimeError("provider dispatch 必须使用可用的 v2 itemized context")
    if not isinstance(context_source_manager, ContextSourceManager):
        raise TypeError("provider context 需要唯一 ContextSourceManager")
    # prepare 冻结身份（seal_input_hash 的一项）：只进注册表与 seal 提交记录，不进 request_hash。
    input_hash = sha256_jcs({
        "schema": "provider-prepare-input:v3",
        "request_messages": [message.model_dump(mode="json") for message in request_messages],
        "wire_profile": asdict(wire_profile),
        "wire_params": normalize_wire_request(wire_params),
        "projector_id": WIRE_PROJECTOR_ID,
        "projector_version": WIRE_PROJECTOR_VERSION,
        "summary_candidate": summary_candidate.identity() if summary_candidate is not None else None,
    })
    plan_id = prepared_context_plan_id(plan_creation_idempotency_key)
    with owner._lock:
        registered = sealed_context if sealed_context is not None else owner.find_sealed_context_for_provider_creation(
            session_id, thread_id, plan_creation_idempotency_key=plan_creation_idempotency_key,
            checkpoint_ns=checkpoint_ns,
        )
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
            )
        else:
            # 成功 SQL 行仅沿本次 prepare 传给 seal writer 重读，不跨请求保存。
            validated_predecessors: _ValidatedPredecessors = {}
            # Registry 等待期间 canonical 可能推进；封存前复用唯一边界端口重新检查。
            conflict, previous = owner.prepare_model_call_boundary(
                session_id, thread_id, turn_id=turn_id, execution_id=execution_id,
                messages=summary_candidate.canonical_messages if summary_candidate is not None else request_messages,
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
            frozen_skills = skill_catalog_from_activation(
                activation_snapshot, lambda frozen, binding: owner.read_resource_activation_payload(
                    frozen, binding, checkpoint_ns=checkpoint_ns,
                    storage_owner=(session_id, thread_id),
                ),
            )
            if frozen_skills is not None:
                install_frozen_skill_catalog(
                    context_source_manager, frozen_skills,
                    include_instructions="skill_load" in tool_set.tool_names,
                )
            rebuild = None if summary_candidate is not None else owner.load_context_source_rebuild_state(
                ContextSourceOwnerKey(session_id, thread_id), checkpoint_ns=checkpoint_ns,
            )

            def read_agents_committed_activation() -> ResourceActivationSnapshotRef:
                source_assembly = rebuild.source_assembly if rebuild is not None else previous
                if source_assembly is None:
                    raise RuntimeError("resource-activation-unavailable: 已 applied AGENTS chain 缺少封存前驱")
                return owner.load_sealed_resource_activation(
                    session_id, thread_id=thread_id, assembly_id=source_assembly.assembly_id,
                    plan_hash=source_assembly.plan_hash, request_hash=source_assembly.request_hash,
                    checkpoint_ns=checkpoint_ns,
                )

            install_frozen_workspace_agents(
                context_source_manager, activation_snapshot,
                lambda frozen, binding: owner.read_resource_activation_payload(
                    frozen, binding, checkpoint_ns=checkpoint_ns,
                    storage_owner=(session_id, thread_id),
                ),
                read_agents_committed_activation,
            )
            frozen = (
                context_source_manager.prepare_sources(rebuild)
                if summary_candidate is None else context_source_manager.prepare_sources()
            )
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
                retained_items = owner._storage.checkpoints.precompaction_context_items(read_snapshot) if summary_candidate is None else ()
                canonical_manifest = read_snapshot.manifest
            # 导入 assembly 是目标本地历史，不是目标已 applied 的请求前缀。
            # 首次请求只携带实际复制并封存的来源，不回读源会话或未复制正文。
            retained = rebuild.source_assembly if rebuild is not None else previous if initial_assembly else None
            retained_refs = rebuild.retained_source_refs if rebuild is not None else frozenset(
                (ref_id, identity[1]) for ref_id, identity in _frozen_source_refs(retained)[0].items()
            ) if retained is not None else frozenset()
            batch = _activation_guidance_batch(
                owner, activation_snapshot, previous, batch, checkpoint_ns=checkpoint_ns,
            )
            if summary_candidate is None:
                def read_payload(frozen, binding):
                    return owner.read_resource_activation_payload(
                        frozen, binding, storage_owner=(session_id, thread_id), checkpoint_ns=checkpoint_ns,
                    )

                @cache
                def resolve_discovery(ref: DiscoveredToolRef) -> FrozenToolDefinition:
                    original, original_call = owner.load_tool_call_resource_activation(
                        session_id, thread_id=thread_id, tool_call_id=ref.tool_call_id,
                        model_call_id=ref.model_call_id, checkpoint_ns=checkpoint_ns,
                    )
                    if original.activation_snapshot_id != ref.activation_snapshot_id:
                        raise ExtensionCatalogUnavailableError("发现结果与原工具调用 activation 不一致")
                    catalog = tool_catalog_from_activation(original, read_payload, model_call_id=original_call)
                    definition = next((value for value in catalog.definitions if value.metadata.tool_id == ref.tool_id), None)
                    if (definition is None or definition.provenance.resource_id != ref.resource_id
                            or definition.provenance.revision != ref.revision):
                        raise ExtensionCatalogUnavailableError("发现结果引用的原冻结定义缺失或身份冲突")
                    return definition

                guidance = next((source.content for source in batch.sources
                                 if source.descriptor.source_id == MCP_GUIDANCE_SOURCE_ID), None)
                automatic = frozenset(entry["tool_name"] for entry in json.loads(guidance)["automatic_definitions"]) if guidance else frozenset()
                batch = tool_definition_batch(
                    batch, catalog_supplier=lambda: tool_catalog_from_activation(
                        activation_snapshot, read_payload, model_call_id=model_call_id,
                    ), current_items=items, retained_items=retained_items,
                    previous=previous, retained_assembly=retained if retained is not None else previous,
                    retained_source_refs=retained_refs if retained is not None else None,
                    resolve_discovery=resolve_discovery, policies_for=extension_policy_resolver,
                    automatic_tool_ids=automatic, restoring=rebuild is not None or initial_assembly,
                )
            if summary_candidate is not None and summary_candidate.minimal_system:
                # 最小摘要只携带本次 owner 产生的受信摘要规则；不沿用常规
                # Agent 指引、overlay 或尚未 applied 的来源通知。
                batch = PendingContextSourceBatch(deltas=(), sources=(ContextSourceSnapshot(
                    descriptor=ContextSourceDescriptor(
                        source_id="compaction-summary-rules", source_kind="summarization",
                        name="摘要规则", description="摘要候选最小系统提示",
                        internal_locator="runtime:compaction-summary-rules", root_placement="root_eligible",
                    ), revision="1", content="Summarize the provided conversation. Return only a concise factual plain-text summary. Do not call tools.",
                    applied_revision=None, tracked=False,
                ),))
            elif summary_candidate is not None:
                batch = PendingContextSourceBatch(deltas=(), sources=tuple(
                    source for source in batch.sources if source.descriptor.root_placement == "root_eligible"
                ))
            bodies = {}
            for contribution in source_contributions(batch):
                owner.register_context_contribution(
                    session_id, thread_id, contribution, checkpoint_ns=checkpoint_ns,
                    request_content=contribution.body,
                )
                bodies[contribution.contribution_id] = contribution.body
            with owner._context_reader.open_snapshot(session_id, thread_id, checkpoint_ns) as read_snapshot:
                if read_snapshot.manifest != canonical_manifest:
                    raise RuntimeError("context-prepare-state-conflict: 来源登记期间 canonical/view 已推进，请重新准备")
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
                validate_tool_protocol_closure(read_snapshot.connection, sequences, len(sequences))
            overlay_bodies = {}
            for ref in full_plan.refs:
                if ref.base_delta_role not in {"base", "delta"} or ref.availability != "available":
                    continue
                body = owner._draft_request_body(
                    session_id, thread_id, checkpoint_ns, full_plan, ref,
                )
                contribution = resolve_contribution_for_ref(ref, full_plan.contributions)
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
            if notice_acceptance is not None and summary_candidate is None:
                applied_ids = set() if previous is None or previous.seal_binding is None else {
                    frame.item_ref.ref_id for frame in previous.seal_binding.prefix.state.frames
                    if frame.item_ref.ref_type == "canonical_item"
                }
                if transition is not None:
                    transition = replace(transition, notice_acceptance=notice_acceptance)
                elif applied_ids & excluded_notices:
                    transition = PendingPrefixEpochTransition(
                        transition_id=f"epoch-notice:{notice_acceptance.acceptance_commit_id}",
                        reason=EpochReason.RUNTIME_NOTICE_CONSUMED, turn_id=turn_id,
                        notice_acceptance=notice_acceptance,
                    )
            if summary_candidate is not None:
                compile_plan = replace(full_plan, refs=tuple(
                    ref for ref in full_plan.refs if ref.ref_type != "canonical_item"
                    and ref.base_delta_role not in {"base", "delta"}
                ))
            plan, preparation = compile_prefix_context(
                compile_plan, () if summary_candidate is not None else items, batch, tool_set, overlay_bodies=overlay_bodies,
                previous=None if summary_candidate is not None else previous,
                initial_assembly=initial_assembly if summary_candidate is None else False,
                transition=transition,
                retained_source_assembly=None if summary_candidate is not None else retained,
                retained_source_refs=frozenset() if summary_candidate is not None else retained_refs,
                retained_source_bodies=retained_bodies,
                provider_profile=f"{target_format}:{provider_version}", turn_id=turn_id,
                notice_acceptance=notice_acceptance,
                allow_provider_reprojection=allow_provider_reprojection,
                deselected_source_ids=context_source_manager.deselected_skill_source_ids(),
            )
            omitted = ()
            if summary_candidate is not None:
                candidate_refs, candidate_sources, preparation, candidate_bodies = compile_summary_candidate(
                    plan, preparation, request_messages, items, summary_candidate,
                )
                # 候选只有当前 plan 的显式 refs；registry 分配稳定顺序，
                # backing_only 阻止后继常规模型请求自动选入摘要副本。
                registered_sources = []
                for source in candidate_sources:
                    if source.source_kind == "summary_candidate":
                        source_ordinal = owner._storage.assemblies.register_context_contribution(
                            session_id, thread_id, source, checkpoint_ns=checkpoint_ns,
                        )
                        source = replace(source, source_ordinal=source_ordinal)
                    registered_sources.append(source)
                bodies.update(candidate_bodies)
                candidate_ids = {
                    item_id for body in candidate_bodies.values()
                    for item_id in (*body["item_ids"], *body["retry_source_item_ids"])
                }
                backing = tuple(ContextRef.canonical_item(
                    item, session_id=session_id, thread_id=thread_id,
                ) for item in items if item.item_id in candidate_ids)
                plan = replace(plan, refs=(*candidate_refs, *backing),
                               contributions=tuple(registered_sources),
                               selection_policy="summary_candidate")
                omitted = tuple(selection_ref_identity(ref) for ref in backing)
            registered = owner.create_context_plan(
                session_id, replace(plan, plan_creation_idempotency_key=plan_creation_idempotency_key),
                checkpoint_ns=checkpoint_ns,
            )
            sealed = seal_registered_plan(
                owner, session_id, thread_id, registered.draft,
                # 复用本次创建返回值；写事务仍重新读取并校验当前 registry/CAS。
                registered=registered,
                validated_predecessors=validated_predecessors,
                seal_idempotency_key=seal_idempotency_key,
                assembly_options={
                    "turn_id": turn_id, "execution_id": execution_id,
                    "provider_version": provider_version, "model_call_id": model_call_id,
                    "target_format": target_format, "loss": (), "omitted_ref_identities": frozenset(omitted),
                },
                wire_profile=wire_profile, wire_params=wire_params,
                request_input_hash=input_hash,
                request_only_content=bodies, context_preparation=preparation,
                activation_snapshot=activation_snapshot,
                checkpoint_ns=checkpoint_ns,
            )
            snapshot, projection = sealed.snapshot, sealed.projection
            if projection is None:
                raise RuntimeError("source-mismatch: Provider seal 未返回本次 wire projection")
            if summary_candidate is None:
                context_source_manager.acknowledge_sealed_pending(csm_batch, snapshot.seal_binding)
        plan = snapshot.as_sealed_plan()
        preimage = normalize_wire_request(finalize_provider_wire(
            messages=projection.messages, tools=projection.tools,
            native_request=None if projection.native is None else projection.native["request"],
            profile=wire_profile, params=wire_params,
        ))
        if canonical_json_bytes(snapshot.request_hash_preimage) != canonical_json_bytes(preimage):
            raise ValueError("source-mismatch: 最终 Provider request 与封存 request hash preimage 不一致")
        pending = {
            "assembly_id": snapshot.assembly_id, "execution_id": snapshot.execution_id,
            "snapshot": snapshot, "plan": plan, "messages": projection.messages,
            "tools": projection.tools, "losses": projection.losses,
            "resource_refs": projection.resource_refs,
        }
        if projection.native is not None:
            pending["native_projection"] = projection.native
        return pending


__all__ = ["prepare_registered_context", "prepared_context_plan_id"]
