"""已注册 draft 的 Saver seal 协调；随机 identity 只能在重试校验之后分配。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, replace
from uuid import uuid4

from app.agents.providers.wire_finalize import (
    WIRE_PROJECTOR_ID,
    WIRE_PROJECTOR_VERSION,
    ChatWireProfile,
    ResponsesWireProfile,
    finalize_provider_wire,
)
from app.domain.itemized.assembly_snapshot import (
    ContextAssemblySnapshot,
    context_request_hash,
)
from app.domain.itemized.epoch.seal_binding import ContextSealPreparation
from app.domain.itemized.identity.refs import ToolSetRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.seal.cleanup import (
    record_failed_seal,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    committed_retry,
    require_key,
    require_registered_draft,
    runtime_draft,
    seal_input_hash,
)
from app.services.infrastructure.rollout_context.provider.native_request import (
    project_native_request,
)
from app.services.infrastructure.rollout_context.provider.toolset_request_bridge import (
    project_tool_set_ref,
)
from app.services.mapping.itemized.prefix_frames import project_sealed_prefix


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
    activation_snapshot=None,
    context_preparation: ContextSealPreparation | None = None,
) -> ContextAssemblySnapshot:
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
            owner, session_id, thread_id, plan, checkpoint_ns
        )
        draft = runtime_draft(registered)
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
            return retry
        # activation 正文必须在 assemble 之前准备：正文写入失败不能留下已完成
        # assemble 的中间状态；绑定闭包只在 assembly_sealed 事务内使用。
        activation_binding = None
        if activation_snapshot is not None:
            _, activation_binding = owner.prepare_resource_activation_binding(
                activation_snapshot, checkpoint_ns=checkpoint_ns
            )
        assembly_id = f"assembly-{uuid4().hex}"
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
                    if entry.included and entry.ref.ref_type == "canonical_item"
                )
                items = owner._storage.read_items(
                    session_id, thread_id, item_ids=item_ids, checkpoint_ns=checkpoint_ns,
                )
                bodies = owner._request_content_for_plan(
                    session_id, thread_id, checkpoint_ns, sealed_plan, None,
                    prepared_details=prepared_details,
                )
                if isinstance(wire_profile, ResponsesWireProfile):
                    native_request = project_native_request(
                        sealed_plan, items, bodies, seal_binding=snapshot.seal_binding,
                    )["request"]
                    messages, tools = (), ()
                else:
                    native_request = None
                    messages = project_sealed_prefix(
                        sealed_plan, snapshot.seal_binding, items, bodies,
                    )
                    tools = [
                        tool for entry in snapshot.selection
                        if entry.included and isinstance(entry.ref, ToolSetRef)
                        for tool in project_tool_set_ref(entry.ref, target_format=snapshot.target_format)
                    ]
                preimage = normalize_wire_request(finalize_provider_wire(
                    messages=messages, tools=tools, native_request=native_request,
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
        owner.seal_context_assembly(
            session_id,
            thread_id,
            snapshot,
            seal_idempotency_key=seal_idempotency_key,
            seal_input_hash=input_hash,
            checkpoint_ns=checkpoint_ns,
            activation_binding=activation_binding,
            prepared_details=prepared_details,
        )
        return owner._storage.get_context_assembly(
            session_id,
            thread_id,
            assembly_id=assembly_id,
            checkpoint_ns=checkpoint_ns,
        )
