"""正向来源准备、唯一 assembly seal 与封存后确认的 Provider 边界。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace

from langchain_core.messages import BaseMessage

from app.agents.providers.wire_finalize import (
    WIRE_PROJECTOR_ID,
    WIRE_PROJECTOR_VERSION,
    ChatWireProfile,
    ResponsesWireProfile,
    finalize_provider_wire,
)
from app.agents.skill_runtime import install_frozen_skill_catalog
from app.core.execution_admission_context import require_current_execution_admission
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.request_plan import resolve_contribution_for_ref
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.mcp.catalog_activation import (
    MCP_GUIDANCE_SOURCE_ID,
    MCP_TOOL_CATALOG_RESOURCE_ID,
    mcp_guidance_content,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    validate_tool_protocol_closure,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    require_key,
    runtime_draft,
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
from app.services.infrastructure.rollout_context.runtime.prefix_compiler import (
    compile_prefix_context,
    source_contributions,
)
from app.services.infrastructure.team.activation import (
    TEAM_FACETS,
    TEAM_RESOURCE_IDS,
    TEAM_SOURCE_IDS,
    team_guidance_content,
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
    provider_version: str, wire_profile: ChatWireProfile | ResponsesWireProfile,
    wire_params: Mapping[str, object],
    target_format: str, checkpoint_ns: str,
    model_call_id: str,
    activation_snapshot: ResourceActivationSnapshotRef,
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
    })
    plan_id = prepared_context_plan_id(plan_creation_idempotency_key)
    with owner._lock:
        registered = owner._find_runtime_context_plan(session_id, thread_id, plan_id, checkpoint_ns)
        if registered is not None and registered.plan_state == "sealed":
            if registered.seal_idempotency_key != seal_idempotency_key:
                raise ValueError("assembly-idempotency-conflict: 已绑定另一个 seal key")
            snapshot = owner._storage.assemblies.get_context_assembly(
                session_id, thread_id, assembly_id=registered.assembly_id, checkpoint_ns=checkpoint_ns,
            )
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
        else:
            # 只有新请求读取 desired；已 sealed/in-flight attempt 完全复用旧 binding。
            tool_set = tool_set_supplier()
            if not isinstance(tool_set, ToolSetSnapshot):
                raise TypeError("Tool producer 必须返回正向 ToolSetSnapshot")
            if turn_id is not None:
                owner._storage.checkpoints.ensure_active_view_contains_turn_root(
                    session_id, thread_id, turn_id=turn_id, checkpoint_ns=checkpoint_ns,
                )
            owner.ensure_request_items(
                session_id, thread_id, turn_id=turn_id, execution_id=execution_id,
                messages=request_messages, checkpoint_ns=checkpoint_ns,
            )
            # 先收敛真实 call/result group；未闭合时不得冻结或登记恢复 source。
            with owner._context_reader.open_snapshot(session_id, thread_id, checkpoint_ns) as read_snapshot:
                sequences = owner.selected_context_message_sequences(read_snapshot)
                validate_tool_protocol_closure(read_snapshot.connection, sequences, len(sequences))
            frozen_skills = skill_catalog_from_activation(
                activation_snapshot, lambda frozen, binding: owner.read_resource_activation_payload(
                    frozen, binding, checkpoint_ns=checkpoint_ns,
                ),
            )
            if frozen_skills is not None:
                install_frozen_skill_catalog(
                    context_source_manager, frozen_skills,
                    include_instructions="read_file" in tool_set.tool_names,
                )
            rebuild = owner.load_context_source_rebuild_state(
                ContextSourceOwnerKey(session_id, thread_id), checkpoint_ns=checkpoint_ns,
            )
            frozen = (
                context_source_manager.prepare_rebuild_sources(rebuild)
                if rebuild is not None else context_source_manager.prepare_sources()
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
                previous = owner._storage.assemblies.latest_context_assembly(read_snapshot)
            batch = _activation_guidance_batch(
                owner, activation_snapshot, previous, batch, checkpoint_ns=checkpoint_ns,
            )
            bodies = {}
            for contribution in source_contributions(batch):
                owner.register_context_contribution(
                    session_id, thread_id, contribution, checkpoint_ns=checkpoint_ns,
                    request_content=contribution.body,
                )
                bodies[contribution.contribution_id] = contribution.body
            with owner._context_reader.open_snapshot(session_id, thread_id, checkpoint_ns) as read_snapshot:
                previous = owner._storage.assemblies.latest_context_assembly(read_snapshot)
                full_plan = owner.compose_committed_context_plan(
                    session_id, thread_id, plan_id=plan_id, tool_set=tool_set,
                    checkpoint_ns=checkpoint_ns, read_snapshot=read_snapshot,
                )
                items = owner._storage.checkpoints.committed_context_items(read_snapshot)
                sequences = owner.selected_context_message_sequences(read_snapshot)
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
            retained = rebuild.source_assembly if rebuild is not None else None
            retained_refs = rebuild.retained_source_refs if rebuild is not None else frozenset()
            retained_bodies = {}
            if retained is not None and retained_refs:
                retained_bodies = owner._request_content_for_plan(
                    session_id, thread_id, checkpoint_ns, retained.as_sealed_plan(), None,
                )
                bodies.update(retained_bodies)
            plan, preparation = compile_prefix_context(
                full_plan, items, batch, tool_set, overlay_bodies=overlay_bodies,
                previous=previous,
                transition=rebuild.transition if rebuild is not None else None,
                retained_source_assembly=retained,
                retained_source_refs=retained_refs,
                retained_source_bodies=retained_bodies,
                provider_profile=f"{target_format}:{provider_version}", turn_id=turn_id,
            )
            if previous is not None:
                bodies.update(owner._request_content_for_plan(
                    session_id, thread_id, checkpoint_ns, previous.as_sealed_plan(), None,
                ))
            registered = owner.create_context_plan(
                session_id, replace(plan, plan_creation_idempotency_key=plan_creation_idempotency_key),
                checkpoint_ns=checkpoint_ns,
            )
            snapshot = owner.seal_context_plan(
                session_id, thread_id, runtime_draft(registered),
                seal_idempotency_key=seal_idempotency_key, turn_id=turn_id,
                execution_id=execution_id, provider_version=provider_version,
                wire_profile=wire_profile, wire_params=wire_params,
                target_format=target_format, request_input_hash=input_hash,
                request_only_content=bodies, context_preparation=preparation,
                model_call_id=model_call_id, activation_snapshot=activation_snapshot,
                checkpoint_ns=checkpoint_ns,
            )
            context_source_manager.acknowledge_sealed_pending(csm_batch, snapshot.seal_binding)
        plan = snapshot.as_sealed_plan()
        messages, tools, losses = owner.project_context_plan_to_provider(
            session_id, thread_id, plan, target_format=target_format,
            checkpoint_ns=checkpoint_ns, include_runtime_notices=True, include_summaries=True,
        )
        native_request = None
        if target_format == "responses":
            native = owner.project_context_plan_to_native(
                session_id, thread_id, plan, checkpoint_ns=checkpoint_ns,
            )
            native_request = native["request"]
        preimage = normalize_wire_request(finalize_provider_wire(
            messages=messages, tools=tools, native_request=native_request,
            profile=wire_profile, params=wire_params,
        ))
        if canonical_json_bytes(snapshot.request_hash_preimage) != canonical_json_bytes(preimage):
            raise ValueError("source-mismatch: 最终 Provider request 与封存 request hash preimage 不一致")
        pending = {
            "assembly_id": snapshot.assembly_id, "execution_id": snapshot.execution_id,
            "snapshot": snapshot, "plan": plan, "messages": messages, "tools": tools, "losses": losses,
        }
        queue = owner._prepared_dispatches.setdefault((session_id, thread_id, checkpoint_ns, execution_id), [])
        if not any(item["assembly_id"] == snapshot.assembly_id for item in queue):
            queue.append(pending)
        return pending


__all__ = ["prepare_registered_context", "prepared_context_plan_id"]
