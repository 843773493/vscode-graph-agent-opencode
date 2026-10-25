"""静态历史与 Saver 测试共用正式 Provider 冻结前置，不发送网络请求。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from uuid import UUID

from app.agents.providers.wire_finalize import ChatWireProfile, ResponsesWireProfile
from app.core.execution_admission_context import require_current_execution_admission
from app.core.identifier import create_uuid_hex
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
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
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from tests.support.custom_tool_test_workspace import (
    FixtureStageObserver,
    timed_fixture_stage,
)


@contextmanager
def _observe_saver_reads(
    saver: RolloutCheckpointSaver,
    observer: FixtureStageObserver | None,
    *,
    session_id: str,
    thread_id: str,
    turn_id: str | None,
) -> Iterator[None]:
    if observer is None:
        yield
        return

    method_names = (
        "find_sealed_context_for_provider_creation",
        "load_context_source_control_states",
        "load_context_source_rebuild_state",
        "get_context_plan_registration",
    )
    instance_attributes = vars(saver)
    patched: list[tuple[str, object, bool]] = []
    try:
        for method_name in method_names:
            original = getattr(saver, method_name)
            had_instance_override = method_name in instance_attributes
            previous_instance_value = instance_attributes.get(method_name)

            def observe(
                *args: object,
                _name: str = method_name,
                _original: Callable[..., object] = original,
                **kwargs: object,
            ) -> object:
                caller = sys._getframe(1)
                with timed_fixture_stage(
                    observer,
                    f"provider.read.{_name}",
                    session_id=session_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    callsite_module=caller.f_globals.get("__name__", ""),
                    callsite_function=caller.f_code.co_name,
                ) as result_fields:
                    result = _original(*args, **kwargs)
                    if result_fields is not None:
                        if _name == "find_sealed_context_for_provider_creation":
                            result_fields["hit"] = result is not None
                        elif _name == "load_context_source_control_states":
                            result_fields["returned_rows"] = len(result) if isinstance(result, tuple) else 0
                        elif _name == "load_context_source_rebuild_state":
                            states = getattr(result, "states", ())
                            result_fields["pending_present"] = result is not None
                            result_fields["returned_rows"] = len(states) if isinstance(states, tuple) else 0
                        else:
                            plan_state = getattr(result, "plan_state", None)
                            result_fields["hit"] = result is not None
                            result_fields["plan_state"] = plan_state if isinstance(plan_state, str) else None
                    return result

            setattr(saver, method_name, observe)
            patched.append((method_name, previous_instance_value, had_instance_override))
        yield
    finally:
        for method_name, previous_instance_value, had_instance_override in reversed(patched):
            if had_instance_override:
                setattr(saver, method_name, previous_instance_value)
            else:
                delattr(saver, method_name)


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


def create_frozen_provider_activation(
    observer: FixtureStageObserver | None = None,
) -> Callable[..., dict[str, object]]:
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
        with timed_fixture_stage(
            observer,
            "provider.activation",
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
                with timed_fixture_stage(
                    observer,
                    "provider.activation.load_sealed_resource_activation",
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
                timed_fixture_stage(
                    observer,
                    "provider.activation.freeze_capture_commit",
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
    observer: FixtureStageObserver | None = None,
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
            _observe_saver_reads(
                saver,
                observer,
                session_id=session,
                thread_id=thread,
                turn_id=admission.turn_id,
            ),
            timed_fixture_stage(
                observer,
                "provider.prepare_total",
                session_id=session,
                thread_id=thread,
                turn_id=admission.turn_id,
                execution_id=admission.execution_id,
                plan_key=key,
            ),
        ):
            registered = saver.find_sealed_context_for_provider_creation(
                session, thread, plan_creation_idempotency_key=key,
            )
            snapshot = None if registered is None else registered.sealed_snapshot
            if snapshot is None:
                with timed_fixture_stage(
                    observer,
                    "provider.context_source_manager",
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
                with timed_fixture_stage(
                    observer,
                    "provider.prepare_context_for_provider",
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
            with timed_fixture_stage(
                observer,
                "provider.register_model_call",
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
