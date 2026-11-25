"""静态历史与 Saver 测试共用正式 Provider 冻结前置，不发送网络请求。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from uuid import UUID

from app.agents.providers.wire_finalize import ChatWireProfile, ResponsesWireProfile
from app.core.execution_admission_context import (
    execution_admission_scope,
    require_current_execution_admission,
)
from app.core.identifier import create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import (
    DerivedExecutionReference,
    InternalExecutionIntent,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.summary_compiler import (
    compile_summary_request,
    summary_request_contributions,
)
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from tests.harness.performance.observers import observe_calls
from tests.harness.performance.record import stage


def frozen_provider_wire(model: str, target_format: str) -> dict[str, object]:
    """返回供 prepare_context_for_provider 展开的 profile/语义参数，不发送 HTTP。"""
    params: dict[str, object] = {"model": model, "stream": True}
    if target_format == "chat_completions":
        profile = ChatWireProfile(None, "openai", model)
        params["stream_options"] = {"include_usage": True}
    elif target_format == "responses":
        profile = ResponsesWireProfile(None, "openai", model)
        params.update({"include": [], "store": False})
    else:
        raise ValueError("测试未定义 Provider 协议: " + target_format)
    return {
        "provider_version": profile.identity(),
        "wire_profile": profile,
        "wire_params": params,
        "target_format": target_format,
    }


def create_frozen_provider_activation() -> Callable[..., dict[str, object]]:
    """在真实 admission 下 freeze_execution→capture→commit 的参数工厂。

    admission 的归属和状态仍由 execution owner 管理。
    """
    model_call_ids_by_execution: dict[tuple[str, str, str, str], str] = {}

    def create(
        saver: RolloutCheckpointSaver,
        *,
        plan_creation_idempotency_key: str,
        checkpoint_ns: str = "",
        model_call_id: str | None = None,
        registry: ResourceRegistry | None = None,
        policy: ResourceActivationPolicySnapshot | None = None,
    ) -> dict[str, object]:
        admission = require_current_execution_admission()
        with stage("provider.activation",
            session_id=admission.session_id,
            thread_id=admission.thread_id,
            turn_id=admission.turn_id,
            execution_id=admission.execution_id,
        ):
            registered = saver.find_sealed_context_for_provider_creation(
                admission.session_id, admission.thread_id,
                plan_creation_idempotency_key=plan_creation_idempotency_key,
                checkpoint_ns=checkpoint_ns,
            )
            if registered is not None:
                sealed = registered.sealed_snapshot
                assert sealed is not None
                UUID(sealed.model_call_id)
                if model_call_id is not None and model_call_id != sealed.model_call_id:
                    raise ValueError("测试 sealed retry 必须复用原 model-call UUID")
                with stage("provider.activation.load_sealed_resource_activation",
                    session_id=admission.session_id,
                    thread_id=admission.thread_id,
                    turn_id=admission.turn_id,
                ):
                    frozen = saver.load_sealed_resource_activation(
                        admission.session_id, thread_id=admission.thread_id,
                        assembly_id=sealed.assembly_id, plan_hash=sealed.plan_hash,
                        request_hash=sealed.request_hash, checkpoint_ns=checkpoint_ns,
                    )
                return {"model_call_id": sealed.model_call_id, "activation_snapshot": frozen}
            execution_plan_key = (
                admission.session_id, admission.thread_id, admission.execution_id,
                plan_creation_idempotency_key,
            )
            call_id = model_call_id
            if call_id is None:
                call_id = model_call_ids_by_execution.get(execution_plan_key)
                if call_id is None:
                    call_id = str(UUID(create_uuid_hex()))
                    model_call_ids_by_execution[execution_plan_key] = call_id
            UUID(call_id)

            async def freeze():
                coordinator = ResourceActivationCoordinator(
                    registry=registry if registry is not None else ResourceRegistry(),
                    saver=saver,
                    body_store=ResourceActivationBodyStore(saver._storage, saver._detail_store),
                )
                parent = await coordinator.freeze_execution(
                    admission=admission,
                    policy=policy if policy is not None else ResourceActivationPolicySnapshot(
                        default_boundary="turn", boundaries={},
                    ),
                    checkpoint_ns=checkpoint_ns,
                )
                return await coordinator.commit_model_call(await coordinator.capture_model_call(
                    parent=parent, model_call_id=call_id, checkpoint_ns=checkpoint_ns,
                ))

            # 同步/异步消费者共用真实 async Coordinator；复制原 scope，收敛线程并透传异常。
            context = copy_context()
            with (
                stage("provider.activation.freeze_capture_commit",
                    session_id=admission.session_id,
                    thread_id=admission.thread_id,
                    turn_id=admission.turn_id,
                ),
                ThreadPoolExecutor(max_workers=1) as executor,
            ):
                frozen = executor.submit(context.run, asyncio.run, freeze()).result()
            return {"model_call_id": call_id, "activation_snapshot": frozen}

    return create


def create_frozen_provider_prepare(
    wire: Callable[[str, str], dict[str, object]],
    activation: Callable[..., dict[str, object]],
) -> Callable[..., ContextAssemblySnapshot]:
    """要求当前真实 admission；key 就是 plan_creation_idempotency_key。

    命中原 sealed snapshot 则复用，否则真实 prepare/seal；最后登记 ModelCall，
    激活当前 admission，不发送 Provider 请求。
    """
    tools = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())

    def prepare(saver: RolloutCheckpointSaver, *, key: str) -> ContextAssemblySnapshot:
        admission = require_current_execution_admission()
        session, thread = admission.session_id, admission.thread_id
        with (
            observe_calls(saver, ("find_sealed_context_for_provider_creation", "load_context_source_control_states", "load_context_source_rebuild_state", "get_context_plan_registration")),
            stage("provider.prepare_total",
                session_id=session,
                thread_id=thread,
                turn_id=admission.turn_id,
                execution_id=admission.execution_id,
                plan_key=key,
            ) as preparation,
        ):
            registered = saver.find_sealed_context_for_provider_creation(
                session, thread, plan_creation_idempotency_key=key,
            )
            snapshot = None if registered is None else registered.sealed_snapshot
            preparation["plan_registration_present"] = registered is not None
            preparation["sealed_snapshot_reused"] = snapshot is not None
            if snapshot is None:
                with stage("provider.context_source_manager",
                    session_id=session,
                    thread_id=thread,
                    turn_id=admission.turn_id,
                ):
                    context_source_manager = ContextSourceManager(
                        owner=ContextSourceOwnerKey(session, thread),
                        control_state_port=saver,
                    )
                wire_arguments = wire("history-fixture", "chat_completions")
                activation_arguments = activation(
                    saver,
                    plan_creation_idempotency_key=key,
                )
                with stage("provider.prepare_context_for_provider",
                    session_id=session,
                    thread_id=thread,
                    turn_id=admission.turn_id,
                    execution_id=admission.execution_id,
                    plan_key=key,
                ):
                    prepared = saver.prepare_context_for_provider(
                        session,
                        thread,
                        turn_id=admission.turn_id,
                        execution_id=admission.execution_id,
                        plan_creation_idempotency_key=key,
                        seal_idempotency_key="seal:" + key,
                        context_source_manager=context_source_manager,
                        tool_set_supplier=lambda: tools,
                        **wire_arguments,
                        **activation_arguments,
                    )
                snapshot = prepared["snapshot"]
                # 静态历史没有 model-start callback，登记前仍须像真实执行器一样
                # 消费 pending handle，不能让长历史累计持有每轮完整请求快照。
                dispatched = saver.consume_prepared_context_for_dispatch(
                    session, thread, execution_id=admission.execution_id,
                    model_call_id=snapshot.model_call_id,
                )
                if dispatched is None or dispatched["assembly_id"] != snapshot.assembly_id:
                    raise AssertionError("历史 fixture 缺少同一 assembly 的派发 handle")
            with stage("provider.register_model_call",
                session_id=session,
                thread_id=thread,
                turn_id=admission.turn_id,
                execution_id=admission.execution_id,
            ):
                saver.register_model_call(
                    session,
                    thread_id=thread,
                    execution_id=admission.execution_id,
                    model_call_id=snapshot.model_call_id,
                    attempt=1,
                    provider="history-fixture",
                    assembly_id=snapshot.assembly_id,
                )
            return snapshot

    return prepare


def create_frozen_summary_prepare(wire, activation):
    """不调用模型，真实登记独立 execution、来源与 sealed assembly。"""
    def prepare(saver, session_id, thread_id, *, key, items, wire_arguments=None, checkpoint_ns=""):
        from dataclasses import replace

        reference = DerivedExecutionReference(f"task:{key}", f"summary-plan:{key}")
        admission = saver.admit_internal_execution(
            session_id, thread_id=thread_id, job_id=f"job:{key}",
            intent=InternalExecutionIntent(
                message_id=f"message:{key}", execution_id=f"execution:{key}",
                admission_idempotency_key=f"admission:{key}", derived_reference=reference,
            ), message=None,
        )
        instruction = "Summarize the provided conversation. Return only a factual plain-text summary. Do not call tools."
        arguments = wire("summary-provider", "chat_completions") if wire_arguments is None else wire_arguments
        with (
            session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id),
            execution_admission_scope(admission, langgraph_thread_id=thread_id),
        ):
            sources = tuple(replace(source, source_ordinal=saver._storage.assemblies.register_context_contribution(
                session_id, thread_id, source, checkpoint_ns=checkpoint_ns,
            )) for source in summary_request_contributions(instruction=instruction))
            plan, preparation, bodies = compile_summary_request(
                session_id=session_id, thread_id=thread_id, plan_id=reference.plan_id,
                provider_profile=f"{arguments['target_format']}:{arguments['provider_version']}",
                instruction=instruction, items=items,
                source_refs=tuple(ContextRef.canonical_item(item, session_id=session_id, thread_id=thread_id) for item in items),
                contributions=sources,
            )
            return saver.prepare_context_for_provider(
                session_id, thread_id, turn_id=None, execution_id=admission.execution_id,
                plan_creation_idempotency_key=f"summary-create:{key}", seal_idempotency_key=f"summary-seal:{key}",
                frozen_plan=plan, context_preparation=preparation, request_only_content=bodies,
                checkpoint_ns=checkpoint_ns, **arguments,
                **activation(saver, plan_creation_idempotency_key=f"summary-create:{key}", checkpoint_ns=checkpoint_ns),
            )

    return prepare
