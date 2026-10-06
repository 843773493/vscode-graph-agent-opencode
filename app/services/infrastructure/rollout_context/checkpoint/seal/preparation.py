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
from app.core.execution_admission_context import require_current_execution_admission
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.request_plan import resolve_contribution_for_ref
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    validate_tool_protocol_closure,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    require_key,
    runtime_draft,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    PendingContextSourceBatch,
)
from app.services.infrastructure.rollout_context.runtime.prefix_compiler import (
    compile_prefix_context,
    source_contributions,
)


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
    plan_id = "plan-prepared:" + sha256_jcs({"creation_key": plan_creation_idempotency_key})
    with owner._lock:
        registered = owner._find_runtime_context_plan(session_id, thread_id, plan_id, checkpoint_ns)
        if registered is not None and registered.plan_state == "sealed":
            if registered.seal_idempotency_key != seal_idempotency_key:
                raise ValueError("assembly-idempotency-conflict: 已绑定另一个 seal key")
            snapshot = owner._storage.get_context_assembly(
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
        else:
            # 只有新请求读取 desired；已 sealed/in-flight attempt 完全复用旧 binding。
            tool_set = tool_set_supplier()
            if not isinstance(tool_set, ToolSetSnapshot):
                raise TypeError("Tool producer 必须返回正向 ToolSetSnapshot")
            frozen = context_source_manager.prepare_sources()
            allowed = {
                source.descriptor.source_id for source in frozen.sources
                if source.descriptor.policy_key is None or source.descriptor.policy_key in tool_set.policy_keys
            }
            batch = PendingContextSourceBatch(
                deltas=tuple(delta for delta in frozen.deltas if delta.source_id in allowed),
                sources=tuple(source for source in frozen.sources if source.descriptor.source_id in allowed),
            )
            if turn_id is not None:
                owner._storage.ensure_active_view_contains_turn_root(
                    session_id, thread_id, turn_id=turn_id, checkpoint_ns=checkpoint_ns,
                )
            owner._storage.ensure_request_items(
                session_id, thread_id, turn_id=turn_id, execution_id=execution_id,
                messages=request_messages, checkpoint_ns=checkpoint_ns,
            )
            bodies = {}
            for contribution in source_contributions(batch):
                owner.register_context_contribution(
                    session_id, thread_id, contribution, checkpoint_ns=checkpoint_ns,
                    request_content=contribution.body,
                )
                bodies[contribution.contribution_id] = contribution.body
            with owner._context_reader.open_snapshot(session_id, thread_id, checkpoint_ns) as read_snapshot:
                previous = owner._storage.latest_context_assembly(read_snapshot)
                full_plan = owner.compose_committed_context_plan(
                    session_id, thread_id, plan_id=plan_id, tool_set=tool_set,
                    checkpoint_ns=checkpoint_ns, read_snapshot=read_snapshot,
                )
                items = owner._storage.committed_context_items(read_snapshot)
                view_id = owner._storage.active_view_id(read_snapshot)
                sequences = owner._storage._view_message_sequences_from_connection(
                    read_snapshot.connection, view_id,
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
            plan, preparation = compile_prefix_context(
                full_plan, items, batch, tool_set, overlay_bodies=overlay_bodies,
                previous=previous,
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
                checkpoint_ns=checkpoint_ns,
            )
            context_source_manager.acknowledge_sealed_pending(batch, snapshot.seal_binding)
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
            "plan": plan, "messages": messages, "tools": tools, "losses": losses,
        }
        queue = owner._prepared_dispatches.setdefault((session_id, thread_id, checkpoint_ns, execution_id), [])
        if not any(item["assembly_id"] == snapshot.assembly_id for item in queue):
            queue.append(pending)
        return pending


__all__ = ["prepare_registered_context"]
