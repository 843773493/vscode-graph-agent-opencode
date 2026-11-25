"""参数化派生任务：首次冻结输入，原图执行，完成事实只从 rollout 冷读。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from boxteam_plugin_sdk.lifecycle import run_cleanup_shielded
from pydantic import JsonValue

from app.abstractions.background_task_registry import BackgroundTaskRegistryProtocol
from app.abstractions.trace_event_sink import TraceEventSinkProtocol
from app.agents.model_call_execution import DerivedModelRequest, ModelCallExecution
from app.agents.model_call_graph import run_derived_agent
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.path_utils import SessionCatalogComponents, workspace_context
from app.core.session_thread_context import session_thread_owner_scope
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.domain.background_work import (
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskStep,
)
from app.domain.itemized.derived_execution import FrozenDerivedInput
from app.domain.itemized.execution_admission import (
    DerivedExecutionReference,
    InternalExecutionIntent,
)
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import ref_identity
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.runtime.provider_model import build_model_from_provider
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.resource_activation import ResourceActivationCoordinator

if TYPE_CHECKING:
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.tools import BaseTool

    from app.services.business.agent_tool_settings.service import (
        AgentToolSettingsService,
    )


class DerivedExecutionHandler:
    kind = "derived_execution"
    version = 1
    effect: Literal["derived_request"] = "derived_request"

    def __init__(self, *, saver: RolloutCheckpointSaver, config: ConfigService,
                 activation: ResourceActivationCoordinator,
                 registry: Callable[[], BackgroundTaskRegistryProtocol], workspace_id: str,
                 workspace_root: Path, session_catalog: SessionCatalogComponents,
                 trace_sink: TraceEventSinkProtocol, tool_settings: Callable[[], AgentToolSettingsService],
                 tool_runtime: Callable[[FrozenDerivedInput], tuple[tuple[BaseTool, ...], tuple[AgentMiddleware, ...]]]) -> None:
        self._saver = saver
        self._config = config
        self._activation = activation
        self._registry = registry
        self._workspace_id, self._workspace_root = workspace_id, workspace_root
        self._session_catalog = session_catalog
        self._tool_settings, self._tool_runtime = tool_settings, tool_runtime
        self._trace_sink = trace_sink

    @staticmethod
    def descriptor_input(descriptor: BackgroundTaskDescriptor) -> tuple[DetailRef, str, datetime]:
        payload = descriptor.payload
        if set(payload) != {"input_ref", "checkpoint_ns", "deadline"}:
            raise ValueError("派生 descriptor 只能保存输入引用、namespace 和绝对期限")
        namespace, deadline = payload["checkpoint_ns"], payload["deadline"]
        if not isinstance(namespace, str) or not isinstance(deadline, str):
            raise TypeError("派生 descriptor 缺少合法 namespace 或期限")
        absolute = datetime.fromisoformat(deadline)
        if absolute.tzinfo is None:
            raise ValueError("派生期限必须带时区")
        return DetailRef.from_dict(payload["input_ref"]), namespace, absolute

    def initial_checkpoint(self, descriptor: BackgroundTaskDescriptor) -> dict[str, JsonValue]:
        self.descriptor_input(descriptor)
        return {}

    def validate_checkpoint(self, checkpoint: dict[str, JsonValue]) -> None:
        if checkpoint:
            raise ValueError("派生任务 checkpoint 不得复制输入或执行事实")

    def deadline(self, handle: BackgroundTaskHandle) -> datetime:
        return self.descriptor_input(handle.descriptor)[2]

    def read_input(self, handle: BackgroundTaskHandle) -> FrozenDerivedInput:
        ref, namespace, deadline = self.descriptor_input(handle.descriptor)
        frozen = self._saver.read_derived_input(handle.session_id, handle.thread_id, ref, checkpoint_ns=namespace)
        if frozen.task_id != handle.task_id or frozen.deadline != deadline:
            raise ValueError("source-mismatch: 派生任务与冻结输入不一致")
        return frozen

    @staticmethod
    def execution_id(frozen: FrozenDerivedInput) -> str:
        return f"derived-execution:{frozen.plan_id}"

    def completed_result(self, frozen: FrozenDerivedInput) -> dict[str, object] | None:
        return self._saver.completed_derived_result(
            frozen.session_id, frozen.thread_id, self.execution_id(frozen), checkpoint_ns=frozen.checkpoint_ns,
        )

    async def reconcile(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep | None:
        frozen = await run_sync_shielded(self.read_input, handle)
        result = await run_sync_shielded(self.completed_result, frozen)
        if result is not None:
            await run_cleanup_shielded(lambda: run_sync_shielded(
                self._saver.release_derived_input, frozen.session_id, frozen.thread_id,
                plan_id=frozen.plan_id, checkpoint_ns=frozen.checkpoint_ns,
            ))
        return None if result is None else BackgroundTaskStep({}, completed=True, result=cast(dict[str, JsonValue], result))

    async def abandon(self, handle: BackgroundTaskHandle, *, outcome: Literal["failed", "cancelled"]) -> None:
        frozen = await run_sync_shielded(self.read_input, handle)
        await run_sync_shielded(
            self._saver.release_derived_input, frozen.session_id, frozen.thread_id,
            plan_id=frozen.plan_id, checkpoint_ns=frozen.checkpoint_ns,
        )

    async def step(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep:
        with workspace_context(self._workspace_id, self._workspace_root, session_catalog=self._session_catalog):
            return await self._execute(handle)

    async def _execute(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep:
        frozen = await run_sync_shielded(self.read_input, handle)
        async with AsyncExitStack() as resources:
            resources.push_async_callback(run_cleanup_shielded, lambda: run_sync_shielded(
                self._saver.release_derived_input, frozen.session_id, frozen.thread_id,
                plan_id=frozen.plan_id, checkpoint_ns=frozen.checkpoint_ns,
            ))
            gateway_token = set_current_gateway_id(frozen.gateway_id)
            resources.callback(reset_current_gateway_id, gateway_token)
            tools, middleware = await run_sync_shielded(self._tool_runtime, frozen)

            def permit(tool_id: str) -> bool:
                policies, _ = self._tool_settings().derived_tools(handle.agent_id, (tool_id,))
                policy = policies[tool_id]
                return policy.execution_enabled and not policy.confirmation_required

            provider = await self._config.prepare_model_recipe(frozen.model_recipe)
            model = await run_sync_shielded(build_model_from_provider, provider, {}, invocation_purpose="derived")
            resources.push_async_callback(run_cleanup_shielded, model.aclose)
            reference = DerivedExecutionReference(frozen.task_id, frozen.plan_id)
            owner = ModelCallExecution(checkpointer=self._saver, session_id=frozen.session_id, thread_id=frozen.thread_id)
            materialized = await run_sync_shielded(
                self._saver._storage.storage.read_ref_items, frozen.session_id, frozen.thread_id,
                checkpoint_ns=frozen.checkpoint_ns, refs=frozen.refs,
            )
            items = tuple(materialized[ref_identity(ref)] for ref in frozen.refs)
            summary = await run_sync_shielded(self._saver.read_derived_summary, frozen)
            plan, preparation, bodies = await run_sync_shielded(
                owner.compile_derived_request, reference=reference, checkpoint_ns=frozen.checkpoint_ns,
                model=model, instruction=frozen.instruction, items=items, source_refs=frozen.refs,
                source_assembly_id=frozen.source_assembly_id,
                initial_sources=frozen.initial_sources,
                initial_tool_set=frozen.initial_tool_set,
                summary=summary,
            )
            admission = await run_sync_shielded(
                self._saver.admit_internal_execution, frozen.session_id, thread_id=frozen.thread_id,
                job_id=f"derived-job:{frozen.plan_id}", message=None, checkpoint_ns=frozen.checkpoint_ns,
                intent=InternalExecutionIntent(message_id=f"derived-message:{frozen.plan_id}",
                    execution_id=self.execution_id(frozen), admission_idempotency_key=f"derived-admission:{frozen.plan_id}",
                    derived_reference=reference),
            )
            with (
                session_thread_owner_scope(MutationIntentOwner(frozen.session_id, frozen.thread_id), langgraph_thread_id=frozen.thread_id),
                execution_admission_scope(admission, langgraph_thread_id=frozen.thread_id),
            ):
                activation = await self._activation.freeze_execution(
                    admission=admission, policy=ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={}),
                    checkpoint_ns=frozen.checkpoint_ns,
                )
                await run_derived_agent(DerivedModelRequest(
                    reference, admission, frozen.checkpoint_ns, model, plan, preparation, bodies,
                    activation, str(UUID(hex=create_uuid_hex(namespace=f"model_call/{frozen.session_id}/{frozen.thread_id}"))),
                ), execution=owner, max_model_calls=frozen.max_model_calls,
                    tools=tools, middleware=middleware, allowed_tool_ids=frozen.allowed_tool_ids,
                    derived_tool_permit=permit, completion_deadline=frozen.deadline,
                    publication_gate=self._registry().derived_publication(handle), trace_sink=self._trace_sink)
            result = await run_sync_shielded(self.completed_result, frozen)
            if result is None:
                raise RuntimeError("派生图返回后缺少完整 canonical 结果")
            return BackgroundTaskStep({}, completed=True, result=cast(dict[str, JsonValue], result))
