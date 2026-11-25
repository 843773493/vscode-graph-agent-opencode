"""唯一 model-call 执行入口：收敛 canonical、准备封存，然后直接发送。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    contextmanager,
    nullcontext,
)
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast, overload
from uuid import UUID

from boxteam_plugin_sdk.lifecycle import run_cleanup_shielded
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain.agents.structured_output import ResponseFormat, ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolCall, ToolMessage
from langchain_core.runnables import Runnable, RunnableBinding

from app.agents.llm_logging_middleware import LLMLoggingMiddleware, ModelCallLogRequest
from app.agents.model_capability_routing import (
    current_selected_provider,
    require_provider_capabilities,
)
from app.agents.policy.tool_capability import ToolCapabilityPolicy, ToolMetadata
from app.agents.providers.wire_finalize import (
    ChatWireProfile,
    ResponsesWireProfile,
    chat_wire_params,
    chat_wire_profile,
    responses_wire_params,
    responses_wire_profile,
)
from app.agents.sealed_assembly_dispatch import sealed_assembly_dispatch_scope
from app.agents.tool_set_producer import RegisteredToolSetProducer
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import require_current_execution_admission
from app.core.failures import report_failure
from app.core.identifier import create_uuid_hex
from app.core.model_delta_context import get_current_model_delta_sink
from app.core.phase_observer import observe_phase, observe_phase_sync
from app.core.shielded_cleanup import run_sync_shielded
from app.domain.context_compaction import (
    CompactionPolicy,
    ContextBudget,
    ContextCapacityError,
    ModelCapacity,
)
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.compaction import (
    ApplyCompactionCandidateIntent,
    CanonicalTextRange,
    FrozenCompactionRange,
    FrozenSummaryResult,
)
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.epoch.seal_binding import ContextSealPreparation
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import (
    DerivedExecutionReference,
    ExecutionAdmission,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.refs import (
    ContextRef,
    ref_identity,
    selection_ref_identity,
)
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.plans.contributions import ContextContribution
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.schemas.internal_v2.session_context import SessionContextResourceDTO
from app.services.infrastructure.mcp.execution_handles import CallRetention
from app.services.infrastructure.rollout_context.assembly.plans.types import (
    ContextPlanRegistration,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    ToolProtocolBoundaryConflict,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_projection import (
    ContextProjectionInput,
    ProviderContextRequest,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.preparation import (
    PreparedProviderContext,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from app.services.orchestration.resource_activation.contracts import (
    require_current_execution_resource_activation,
)

if TYPE_CHECKING:
    from app.runtime.context_compaction import AgentSummarizationCompactor
    from app.services.infrastructure.rollout_context.checkpoint.saver import (
        RolloutCheckpointSaver,
    )

logger = logging.getLogger(__name__)


def _runtime_thread_id(request: ModelRequest[Any]) -> str:
    runtime = request.runtime
    if runtime is None:
        raise RuntimeError("model-call execution 缺少 runtime")
    execution_info = runtime.execution_info
    thread_id = getattr(execution_info, "thread_id", None)
    if not isinstance(thread_id, str) or not thread_id:
        raise RuntimeError("model-call execution 缺少 runtime thread_id")
    return thread_id


def _runtime_checkpoint_ns(request: ModelRequest[Any]) -> str:
    runtime = request.runtime
    if runtime is None:
        raise RuntimeError("model-call execution 缺少 runtime")
    execution_info = runtime.execution_info
    checkpoint_ns = getattr(execution_info, "checkpoint_ns", "")
    if not isinstance(checkpoint_ns, str):
        raise TypeError("model-call execution checkpoint_ns 非法")
    return checkpoint_ns


def _request_idempotency_keys(
    request: ModelRequest[Any],
    *,
    session_id: str,
    turn_id: str | None,
    execution_id: str,
    checkpoint_ns: str,
    provider_profile: str,
) -> tuple[str, str]:
    """以 LangGraph 的持久 task/attempt 身份区分调用，不从 prompt 或 UUID 猜测。"""
    runtime = request.runtime
    info = runtime.execution_info if runtime is not None else None
    checkpoint_id = getattr(info, "checkpoint_id", None)
    task_id = getattr(info, "task_id", None)
    attempt = getattr(info, "node_attempt", None)
    if not all(
        isinstance(value, str) and value
        for value in (checkpoint_id, task_id, execution_id)
    ):
        raise RuntimeError(
            "model-call execution 缺少 checkpoint/task/execution identity"
        )
    if type(attempt) is not int or attempt < 1:
        raise RuntimeError("model-call execution 缺少合法 node_attempt")
    identity = sha256_jcs(
        {
            "schema": "itemized-provider-attempt:v1",
            "session_id": session_id,
            "turn_id": turn_id,
            "execution_id": execution_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
            "task_id": task_id,
            "node_attempt": attempt,
            "provider_profile": provider_profile,
        }
    )
    return f"provider-create:{identity}", f"provider-seal:{identity}"


def _target_format(model: object) -> str:
    name = type(model).__name__.lower()
    return "responses" if "responses" in name else "chat_completions"


def _request_wire_options(bound_model: object) -> dict[str, Any]:
    """复用真实 model binding，提取已经过模型处理的请求参数，不持有 tools。"""
    kwargs = getattr(bound_model, "kwargs", None)
    if not isinstance(kwargs, Mapping):
        raise TypeError("itemized context model binding 缺少有效 kwargs")
    return {key: value for key, value in kwargs.items() if key != "tools"}


@overload
def _bind_response_model_call(response: ModelResponse[Any], *, model_call_id: str, retain_calls: CallRetention | None = None) -> ModelResponse[Any]: ...


@overload
def _bind_response_model_call(response: ExtendedModelResponse[Any], *, model_call_id: str, retain_calls: CallRetention | None = None) -> ExtendedModelResponse[Any]: ...


def _bind_response_model_call(
    response: ModelResponse[Any] | ExtendedModelResponse[Any], *, model_call_id: str,
    retain_calls: CallRetention | None = None,
) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
    """在本次 dispatch 返回边界保留真实 callback 身份，不从 vendor ID 推导。"""
    model_response = response.model_response if isinstance(response, ExtendedModelResponse) else response
    codec = LangChainMessageCodec()
    identity = ItemProjectionIdentity(model_call_id=model_call_id)
    messages = []
    pending_assistants: list[AIMessage] = []
    call_counts: dict[str, int] = {}
    retained_calls: list[ToolCall] = []
    for message in model_response.result:
        if isinstance(message, (AIMessage, ToolMessage)):
            existing = codec.model_call_id(message)
            if existing is not None and existing != model_call_id:
                raise ValueError(
                    "source-mismatch: dispatch response 的 model_call_id 冲突: "
                    f"message_id={message.id!r}, expected={model_call_id!r}, actual={existing!r}"
                )
            if isinstance(message, AIMessage):
                pending_assistants.append(message)
            else:
                # 每份声明只登记一次，避免逐结果重扫全部声明；计数保留重复冲突。
                # 只在结果到达时消费之前的声明，尾部未消费字段沿用原错误边界。
                for assistant in pending_assistants:
                    for call in assistant.tool_calls:
                        if isinstance(call["id"], str):
                            key = provider_tool_call_id(identity, call["id"])
                            call_counts[key] = call_counts.get(key, 0) + 1
                        if retain_calls is not None:
                            retained_calls.append(call)
                pending_assistants.clear()
                if call_counts.get(provider_tool_call_id(identity, message.tool_call_id), 0) != 1:
                    raise ValueError(
                        "source-mismatch: dispatch ToolMessage 缺少唯一 emitting assistant: "
                        f"tool_call_id={message.tool_call_id!r}"
                    )
            message = message.model_copy(update={
                "response_metadata": {**message.response_metadata, "model_call_id": model_call_id},
            })
        messages.append(message)
    bound = replace(model_response, result=messages)
    if retain_calls is not None:
        # 复用配对时已读取的原始声明，只补齐尾部尚未消费的声明，保留原顺序和列表契约。
        retained_calls.extend(call for assistant in pending_assistants for call in assistant.tool_calls)
        retain_calls(retained_calls)
    return replace(response, model_response=bound) if isinstance(response, ExtendedModelResponse) else bound


@dataclass(frozen=True, slots=True)
class DerivedModelRequest:
    """独立 execution 的冻结请求；不持有父 Graph、CSM 或工具目录。"""

    reference: DerivedExecutionReference
    admission: ExecutionAdmission
    checkpoint_ns: str
    model: BaseChatModel
    plan: ContextRequestPlan
    preparation: ContextSealPreparation
    request_only_content: Mapping[str, object]
    activation_snapshot: ResourceActivationSnapshotRef
    model_call_id: str
    model_call_attempt: int = 1
    provider_wait: Callable[[bool], None] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reference, DerivedExecutionReference) or not isinstance(self.admission, ExecutionAdmission):
            raise TypeError("派生模型请求必须有 typed reference 与独立 admission")
        if not isinstance(self.model, BaseChatModel) or not isinstance(self.preparation, ContextSealPreparation):
            raise TypeError("派生模型请求缺少冻结模型或 preparation")
        if self.admission.execution_origin != "internal" or self.admission.turn_id is not None:
            raise ValueError("派生模型请求只能属于无 Turn internal execution")
        if (self.plan.session_id, self.plan.thread_id) != (
            self.admission.session_id, self.admission.thread_id,
        ):
            raise ValueError("source-mismatch: 派生模型请求 plan 与准入引用不一致")
        if type(self.model_call_attempt) is not int or self.model_call_attempt < 1:
            raise ValueError("派生模型调用序号必须为正整数")
        if not isinstance(self.checkpoint_ns, str):
            raise TypeError("派生模型请求 namespace 非法")


@dataclass(frozen=True, slots=True)
class DerivedModelResult:
    """本次模型调用已保存的输出；execution 由原图循环结束时收敛。"""

    response: ModelResponse[Any]
    dispatch_ref: SealedContextDispatchRef
    output_refs: tuple[ContextRef, ...]


@dataclass(frozen=True, slots=True)
class _ProviderPreparation:
    """本次 request 的纯准备值；持久事实始终由 Saver 持有。"""

    admission: ExecutionAdmission
    checkpoint_ns: str
    creation_key: str
    seal_key: str
    provider_version: str
    target_format: str
    wire_profile: ChatWireProfile | ResponsesWireProfile
    wire_params: Mapping[str, object]
    include_structured_output: bool
    bound_model: Runnable[Any, AIMessage]
    response_format: ResponseFormat[Any] | None
    allow_provider_reprojection: bool
    capacity: ModelCapacity


class _PreparedModelCall(TypedDict):
    """本次执行的已验证 Saver receipt 与框架绑定，不写入持久化。"""

    assembly_id: str
    snapshot: ContextAssemblySnapshot
    dispatch_ref: SealedContextDispatchRef
    callback_model_call_id: str
    bound_model: Runnable[Any, AIMessage]
    response_format: ResponseFormat[Any] | None
    messages: list[BaseMessage]
    tools: list[dict[str, object]]
    losses: tuple[str, ...]
    resource_refs: tuple[SessionContextResourceDTO, ...]
    native_projection: NotRequired[Mapping[str, object]]
    extension_call_scope: NotRequired[AbstractContextManager[CallRetention]]
    compaction_target: NotRequired[Mapping[str, object]]
    model_name: NotRequired[str]


@dataclass(frozen=True, slots=True)
class _CompactionPreparation:
    """尚未封存的预算裁决；等待和采用由异步 owner 在原 gate 外协调。"""

    prepared: PreparedProviderContext
    budget: ContextBudget
    candidate: FrozenCompactionRange | None
    policy: CompactionPolicy
    generation: str
    reached_apply: bool
    sufficient: bool = True
    manual_requested: bool = False
    retained_budget: ContextBudget | None = None


@dataclass(slots=True)
class _GraphCompactionState:
    """节点内的采用回执，供同步桥接、fallback 与最终图更新共用。"""

    messages: tuple[BaseMessage, ...] | None = None
    overflow_recovery: bool = False


class ModelCallExecution:
    """按真实 attempt 完成准备并直接消费 Saver 的封存 payload。"""

    def __init__(
        self, *, checkpointer: object, session_id: str, thread_id: str,
        context_source_manager: ContextSourceManager | None = None,
        tool_set_producer: RegisteredToolSetProducer | None = None,
        resource_activation: ResourceActivationCoordinator | None = None,
        source_message_grouping_supplier: Callable[[], Literal["aggregate", "separate"]] = lambda: "aggregate",
        extension_policy_resolver: Callable[[Sequence[ToolMetadata]], Mapping[str, ToolCapabilityPolicy]] | None = None,
        context_read_admission: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        context_control_admission: Callable[[str, Mapping[str, object], str], AbstractAsyncContextManager[None]] | None = None,
        extension_call_scope: Callable[[ResourceActivationSnapshotRef, str, str], AbstractContextManager[CallRetention]] | None = None,
        compaction_runtime: AgentSummarizationCompactor | None = None,
        agent_id: str | None = None,
        compaction_model_recipe: Callable[[BaseChatModel], Mapping[str, object]] | None = None,
    ) -> None:
        if not session_id or not thread_id:
            raise ValueError("model-call execution 需要显式 SessionThread owner")
        # 合成 inspection 图也可装配；真正执行时先校验 Saver 端口及资源准入。
        self._checkpointer = cast("RolloutCheckpointSaver", checkpointer)
        self._session_id = session_id
        self._thread_id = thread_id
        self._context_source_manager = context_source_manager
        self._tool_set_producer = tool_set_producer
        self._resource_activation = resource_activation
        self._source_message_grouping_supplier = source_message_grouping_supplier
        self._extension_policy_resolver = extension_policy_resolver
        self._context_read_admission = context_read_admission
        self._context_control_admission = context_control_admission
        self._extension_call_scope = extension_call_scope
        self._compaction_runtime = compaction_runtime
        self._agent_id = agent_id
        self._compaction_model_recipe = compaction_model_recipe
        self._graph_compaction: ContextVar[_GraphCompactionState | None] = ContextVar("graph_compaction", default=None)
        self._graph_model_binding: Callable[[ModelRequest[Any]], tuple[Runnable[Any, AIMessage], ResponseFormat[Any] | None]] | None = None
        self._decode_output: Callable[..., Mapping[str, Any]] | None = None
        self._model_name: str | None = None

    @contextmanager
    def graph_request_scope(self):
        """一次图节点含所有路由尝试共用采用结果；离开节点立即释放。"""
        state = _GraphCompactionState()
        token = self._graph_compaction.set(state)
        try:
            yield lambda: state.messages
        finally:
            self._graph_compaction.reset(token)

    def bind_model(self, *, bind_model, decode_output, name, structured_tools) -> None:
        """图适配器只注入一次框架的真实绑定、解析和结构化工具注册表。"""
        if self._graph_model_binding is not None:
            raise RuntimeError("model-call execution 已绑定模型")
        if self._tool_set_producer is None:
            raise RuntimeError("Graph 模型绑定缺少注册工具 owner")
        self._tool_set_producer.freeze_structured_output_tools(structured_tools)
        self._graph_model_binding = bind_model
        self._decode_output = decode_output
        self._model_name = name

    def _request_preparation(self, request: ModelRequest[Any]) -> _ProviderPreparation:
        if self._context_source_manager is None or self._tool_set_producer is None or self._context_read_admission is None or self._context_control_admission is None:
            raise RuntimeError("Graph 模型请求缺少来源、工具或 admission gate")
        if request.system_message is not None:
            raise RuntimeError("instruction-producer-unregistered: 最终请求出现未登记 system prompt")
        session_id = self._session_id
        thread_id = _runtime_thread_id(request)
        if thread_id != self._thread_id:
            raise RuntimeError(
                "itemized context runtime thread 与 Agent owner 不一致: "
                f"expected={self._thread_id!r}, actual={thread_id!r}"
            )
        runtime_namespace = _runtime_checkpoint_ns(request)
        checkpoint_ns = self._checkpointer.context_owner_namespace(runtime_namespace)
        admission = require_current_execution_admission(
            langgraph_thread_id=thread_id
        )
        if admission.session_id != session_id or admission.thread_id != thread_id:
            raise RuntimeError("itemized context request 与当前 execution owner 不一致")
        turn_id = admission.turn_id
        if (
            (admission.execution_origin == "user_turn" and turn_id is None)
            or (admission.execution_origin == "internal" and turn_id is not None)
        ):
            raise RuntimeError("itemized context Turn 与 execution admission origin 不一致")
        target_format = _target_format(request.model)
        if self._graph_model_binding is None:
            raise RuntimeError("model-call execution 尚未绑定实际 Agent 图")
        bound_model, response_format = self._graph_model_binding(request)
        include_structured_output = isinstance(response_format, ToolStrategy)
        options = _request_wire_options(bound_model)
        stop = options.pop("stop", None)
        if target_format == "responses":
            if stop is not None:
                raise ValueError("Responses API 不支持 stop 参数")
            wire_profile = responses_wire_profile(request.model)
            wire_params = responses_wire_params(request.model, kwargs=options)
        else:
            wire_profile = chat_wire_profile(request.model)
            # 页面事件流与上游传输分离，封存采用冻结模型的实际传输选择。
            wire_params = chat_wire_params(
                request.model, stop=stop, kwargs=options, stream=wire_profile.streaming,
            )
        # provider_version 只承载实际 wire profile 身份，不用 model name 冒充。
        provider_version = wire_profile.identity()
        allow_provider_reprojection = current_selected_provider() is request.model
        creation_key, seal_key = _request_idempotency_keys(
            request,
            session_id=session_id,
            turn_id=turn_id,
            checkpoint_ns=runtime_namespace,
            execution_id=admission.execution_id,
            provider_profile=f"{target_format}:{provider_version}",
        )
        state = self._graph_compaction.get()
        if state is not None and state.overflow_recovery:
            # 已失败的封存请求保持不变；补压缩后的重发有独立、可重建的身份。
            creation_key += ":overflow-recovery"
            seal_key += ":overflow-recovery"
        logger.warning(
            "[itemized-context] provider prepare entered: session_id=%s turn_id=%s checkpoint_ns=%s",
            session_id,
            turn_id,
            checkpoint_ns,
        )
        logger.warning(
            "[itemized-context] incoming request messages: types=%s ids=%s tool_call_ids=%s",
            [type(message).__name__ for message in request.messages],
            [getattr(message, "id", None) for message in request.messages],
            [
                getattr(message, "tool_call_id", None)
                for message in request.messages
                if hasattr(message, "tool_call_id")
            ],
        )
        return _ProviderPreparation(
            admission=admission, checkpoint_ns=checkpoint_ns,
            creation_key=creation_key, seal_key=seal_key,
            provider_version=provider_version, target_format=target_format,
            wire_profile=wire_profile, wire_params=wire_params,
            include_structured_output=include_structured_output,
            bound_model=bound_model, response_format=response_format,
            allow_provider_reprojection=allow_provider_reprojection,
            capacity=ModelCapacity(
                max_input_tokens=(request.model.profile or {}).get("max_input_tokens"),
                context_window=(request.model.profile or {}).get("context_window"),
            ),
        )

    def _derived_source_inputs(
        self, source_assembly_id: str | None, checkpoint_ns: str, *,
        initial_sources: Sequence[ContextContribution] = (), initial_tool_set: ToolSetSnapshot | None = None,
    ):
        source_snapshot = None
        source_content = {}
        inherited = []
        tool_set = None
        if source_assembly_id is not None:
            with self._checkpointer._context_reader.open_snapshot(self._session_id, self._thread_id, checkpoint_ns) as read:
                source_snapshot = self._checkpointer._storage.assemblies.read_context_assembly(
                    self._session_id, self._thread_id, assembly_id=source_assembly_id, checkpoint_ns=checkpoint_ns, snapshot=read,
                )[0]
                source_content = self._checkpointer._request_content_for_plan(
                    self._session_id, self._thread_id, checkpoint_ns, source_snapshot.as_sealed_plan(), None, snapshot=read,
                )
            binding = source_snapshot.seal_binding
            manifest = source_snapshot.tool_set_refs[0]
            tool_set = ToolSetSnapshot(
                selection_revision=binding.selection_revision, tools=source_snapshot.tool_snapshot,
                policy_keys=manifest.tool_policy["enabled_policy_keys"], policy_version=manifest.tool_policy_version,
            )
            for source in source_snapshot.contributions:
                if source.contribution_id not in source_content:
                    continue
                restored = replace(source, assembly_id=None, contribution_ordinal=None,
                                   body=source_content[source.contribution_id], source_ordinal=None)
                ordinal = self._checkpointer._storage.assemblies.register_context_contribution(
                    self._session_id, self._thread_id, restored, checkpoint_ns=checkpoint_ns,
                )
                inherited.append(replace(restored, source_ordinal=ordinal))
        else:
            tool_set = initial_tool_set
            for source in initial_sources:
                ordinal = self._checkpointer._storage.assemblies.register_context_contribution(
                    self._session_id, self._thread_id, source, checkpoint_ns=checkpoint_ns,
                )
                inherited.append(replace(source, source_ordinal=ordinal))
        return source_snapshot, source_content, inherited, tool_set

    def compile_derived_request(
        self, *, reference: DerivedExecutionReference, checkpoint_ns: str, model: BaseChatModel,
        instruction: str, items: Sequence[CanonicalItemRecord], source_refs: Sequence[ContextRef],
        source_assembly_id: str | None = None,
        initial_sources: Sequence[ContextContribution] = (),
        initial_tool_set: ToolSetSnapshot | None = None,
        summary: FrozenSummaryResult | None = None,
    ) -> tuple[ContextRequestPlan, ContextSealPreparation, dict[str, object]]:
        from app.domain.itemized.hashing import contribution_content_hash
        from app.domain.itemized.plans.contributions import SourceBinding
        from app.prompting import internal_message_factory
        from app.services.infrastructure.rollout_context.runtime.derived_compiler import (
            closed_source_boundaries,
            compile_derived_request,
        )

        if not items or not (boundaries := closed_source_boundaries(items)) or boundaries[-1] != len(items):
            raise ValueError("derived-source-unavailable: 派生执行必须读取冻结闭合范围")
        prepared_instruction = internal_message_factory.build(kind="derived_task_instruction", control=instruction)
        body = prepared_instruction.content
        identity = f"derived-instruction:{sha256_jcs(body)}"
        source = ContextContribution(
            contribution_id=identity, source_kind="derived_request", source_revision=sha256_jcs(body),
            contribution_kind="prompt", body=body, content_hash=contribution_content_hash("prompt", body),
            source_binding=SourceBinding(source_ref=identity), root_placement="tail_only",
        )
        ordinal = self._checkpointer._storage.assemblies.register_context_contribution(
            self._session_id, self._thread_id, source, checkpoint_ns=checkpoint_ns,
        )
        source = replace(source, source_ordinal=ordinal)
        replacement = None
        if summary is not None:
            from app.services.infrastructure.rollout_context.runtime.summary_compiler import (
                summary_result_contribution,
            )

            replacement = summary_result_contribution(summary)
            ordinal = self._checkpointer._storage.assemblies.register_context_contribution(
                self._session_id, self._thread_id, replacement, checkpoint_ns=checkpoint_ns,
            )
            replacement = replace(replacement, source_ordinal=ordinal)
        snapshot, content, inherited, tool_set = self._derived_source_inputs(
            source_assembly_id, checkpoint_ns, initial_sources=initial_sources, initial_tool_set=initial_tool_set,
        )
        target = _target_format(model)
        profile = responses_wire_profile(model) if target == "responses" else chat_wire_profile(model)
        return compile_derived_request(
            session_id=self._session_id, thread_id=self._thread_id, plan_id=reference.plan_id,
            provider_profile=f"{target}:{profile.identity()}", items=items, source_refs=source_refs, instruction=source,
            source_snapshot=snapshot, source_contributions=inherited, source_content=content,
            replacement_message=replacement,
            backing_refs=() if summary is None else summary.output_refs,
            tool_set=tool_set if tool_set is not None else ToolSetSnapshot(selection_revision=sha256_jcs({"tools": []}), tools=()),
        )

    def compile_summary_request(
        self, *, reference: DerivedExecutionReference, checkpoint_ns: str, model: BaseChatModel,
        instruction: str, items: Sequence[CanonicalItemRecord], source_refs: Sequence[ContextRef],
        derived_summaries: Sequence[FrozenSummaryResult] = (), source_assembly_id: str | None = None,
        text_range: CanonicalTextRange | None = None,
        initial_sources: Sequence[ContextContribution] = (), initial_tool_set: ToolSetSnapshot | None = None,
    ) -> tuple[ContextRequestPlan, ContextSealPreparation, dict[str, object]]:
        """来源声明沿唯一 registry 取得真实 ordinal，再交纯 compiler。"""
        from app.services.infrastructure.rollout_context.runtime.summary_compiler import (
            compile_summary_request,
            summary_request_contributions,
        )

        if initial_sources and initial_tool_set is None:
            raise ValueError("summary-source-mismatch: 当前来源快照必须同时冻结真实 ToolSet")
        source_snapshot, source_content, inherited, tool_set = self._derived_source_inputs(
            None if initial_tool_set is not None else source_assembly_id, checkpoint_ns,
            initial_sources=initial_sources, initial_tool_set=initial_tool_set,
        )
        contributions = []
        for source in summary_request_contributions(
            instruction=instruction, derived_summaries=derived_summaries,
        ):
            ordinal = self._checkpointer._storage.assemblies.register_context_contribution(
                self._session_id, self._thread_id, source, checkpoint_ns=checkpoint_ns,
            )
            contributions.append(replace(source, source_ordinal=ordinal))
        target = _target_format(model)
        profile = responses_wire_profile(model) if target == "responses" else chat_wire_profile(model)
        return compile_summary_request(
            session_id=self._session_id, thread_id=self._thread_id, plan_id=reference.plan_id,
            provider_profile=f"{target}:{profile.identity()}", instruction=instruction,
            items=items, source_refs=source_refs, derived_summaries=derived_summaries, contributions=contributions,
            source_snapshot=source_snapshot, source_contributions=inherited, source_content=source_content, tool_set=tool_set,
            text_range=text_range,
        )

    def continue_derived_request(
        self, previous: DerivedModelRequest, result: DerivedModelResult, request: ModelRequest,
    ) -> DerivedModelRequest:
        """工具批次沿原 canonical owner 落盘，再追加到本 execution 已冻结的前缀。"""
        from app.domain.itemized.epoch.prefix_epoch import append_items
        from app.services.infrastructure.rollout_context.runtime.derived_compiler import (
            request_ref,
        )
        from app.services.mapping.itemized.prefix_frames import (
            project_canonical_prefix_frames,
        )

        output = result.response.result[-1]
        if not isinstance(output, AIMessage) or not output.tool_calls:
            raise ValueError("派生后续请求缺少上一批工具声明")
        model_call_id = result.dispatch_ref.sealed_model_call_id
        codec = LangChainMessageCodec()
        if codec.model_call_id(output) != model_call_id:
            raise ValueError("source-mismatch: 派生工具批次与原 ModelCall 不一致")
        trailing = []
        for message in reversed(request.messages):
            if isinstance(message, AIMessage):
                if codec.model_call_id(message) != model_call_id:
                    raise ValueError("source-mismatch: 派生图最后的 assistant 不属于本次调用")
                break
            if not isinstance(message, ToolMessage):
                raise TypeError("派生工具批次中存在非工具结果消息")
            trailing.append(message)
        trailing.reverse()
        declared = [call["id"] for call in output.tool_calls]
        if [message.tool_call_id for message in trailing] != declared:
            raise ValueError("source-mismatch: 派生工具结果必须按原声明完整配对")
        timestamp = datetime.now(UTC).isoformat()
        assistant_items = codec.items_for_message(
            output, item_sequence=1, message_id=output.id or f"derived:{model_call_id}",
            turn_id=None, execution_id=previous.admission.execution_id, timestamp=timestamp,
            model_call_id=model_call_id,
        )
        tool_items = tuple(item for message in trailing for item in codec.items_for_message(
            message, item_sequence=1, message_id=f"derived-tool:{model_call_id}:{message.tool_call_id}",
            turn_id=None, execution_id=previous.admission.execution_id, timestamp=timestamp,
            model_call_id=model_call_id,
        ))
        self._checkpointer._storage.executions.append_items(
            self._session_id, self._thread_id, tool_items,
            execution_id=previous.admission.execution_id, checkpoint_ns=previous.checkpoint_ns,
        )
        stored = self._checkpointer.read_canonical_items(
            self._session_id, self._thread_id, checkpoint_ns=previous.checkpoint_ns,
            item_ids=tuple(item.item_id for item in (*assistant_items, *tool_items)),
        )
        state = previous.preparation.prefix_state
        frames, _selected = project_canonical_prefix_frames(
            stored, provider_profile=state.provider_profile, start_ordinal=len(state.frames),
        )
        state = append_items(state, frames, desired_provider_profile=state.provider_profile,
                             desired_tool_compatibility_key=state.tool_compatibility_key)
        plan = replace(previous.plan, plan_id=f"{previous.reference.plan_id}:after:{model_call_id}", refs=(), tool_set_refs=())
        contributions = {source.contribution_id: source for source in plan.contributions}
        refs = tuple(request_ref(plan, contributions[ref.ref_id]) if ref.ref_type == "request_only" else ref
                     for ref in previous.plan.refs)
        plan = replace(plan, refs=(
            *refs,
            *(ContextRef.canonical_item(item, session_id=self._session_id, thread_id=self._thread_id) for item in stored),
        ), tool_set_refs=(previous.preparation.tool_set.bind(
            session_id=self._session_id, thread_id=self._thread_id, plan_id=plan.plan_id,
        ),))
        return replace(previous, plan=plan, preparation=replace(previous.preparation, prefix_state=state),
                       model_call_id=str(UUID(hex=create_uuid_hex(namespace=f"model_call/{self._session_id}/{self._thread_id}"))),
                       model_call_attempt=previous.model_call_attempt + 1)

    def preview_summary_request(
        self, *, plan: ContextRequestPlan, preparation: ContextSealPreparation,
        bodies: Mapping[str, object], items: Sequence[CanonicalItemRecord], source_refs: Sequence[ContextRef],
        derived_summaries: Sequence[FrozenSummaryResult], model: BaseChatModel, checkpoint_ns: str = "",
    ) -> ProviderContextRequest:
        """纯投影预算值；临时 identity 不登记、不持久化，也不生成 dispatch receipt。"""
        if plan.selection_policy != "derived_request" or len(items) != len(source_refs):
            raise ValueError("summary-source-mismatch: 预算必须使用同一冻结摘要计划")
        target = _target_format(model)
        profile = responses_wire_profile(model) if target == "responses" else chat_wire_profile(model)
        params = responses_wire_params(model, kwargs={}) if target == "responses" else chat_wire_params(model, kwargs={}, stream=profile.streaming)
        assembly_id = "preview-" + sha256_jcs(plan.plan_id).rsplit(":", 1)[1]
        backing = tuple(ref for result in derived_summaries for ref in (*result.coverage, *result.output_refs))
        omitted = tuple(selection_ref_identity(ref) for ref in backing)
        detail_refs = {selection_ref_identity(ref): DetailRef(plan.session_id, plan.thread_id, assembly_id, f"source-{index}")
                       for index, ref in enumerate(plan.refs) if ref.ref_type == "request_only"}
        snapshot = ContextPlanComposer().assembly(
            plan=plan, assembly_id=assembly_id, session_id=plan.session_id, thread_id=plan.thread_id,
            turn_id=None, execution_id=f"preview:{plan.plan_id}", provider_version=profile.identity(),
            target_format=target, omitted_ref_identities=omitted, request_detail_refs=detail_refs,
            seal_binding=preparation.bind(assembly_id),
        )
        materialized = {ref_identity(ref): item for ref, item in zip(source_refs, items, strict=True)}
        if backing:
            materialized.update(self._checkpointer._storage.storage.read_ref_items(
                plan.session_id, plan.thread_id, refs=backing, checkpoint_ns=checkpoint_ns,
            ))
        return self._checkpointer.project_frozen_context_request(
            ContextProjectionInput(snapshot, snapshot.as_sealed_plan(), None), materialized, bodies,
            wire_profile=profile, wire_params=params,
        )

    def _derived_preparation(self, request: DerivedModelRequest) -> _ProviderPreparation:
        admission = require_current_execution_admission(langgraph_thread_id=self._thread_id)
        if admission != request.admission or (admission.session_id, admission.thread_id) != (self._session_id, self._thread_id):
            raise ValueError("source-mismatch: 派生请求必须绑定自己的 execution admission")
        accepted = self._checkpointer.derived_execution_reference(
            self._session_id, thread_id=self._thread_id, execution_id=admission.execution_id,
        )
        if accepted != request.reference:
            raise ValueError("source-mismatch: 派生请求与已接受 compaction reference 不一致")
        if getattr(request.model, "invocation_purpose", None) not in {"derived", "compaction"}:
            raise ValueError("派生请求必须使用独立派生模型，禁止隐藏 SDK 重试")
        bound_model = request.model.bind()
        target_format = _target_format(request.model)
        profile = responses_wire_profile(request.model) if target_format == "responses" else chat_wire_profile(request.model)
        params = responses_wire_params(request.model, kwargs={}) if target_format == "responses" else chat_wire_params(
            request.model, kwargs={}, stream=profile.streaming,
        )
        if "response_format" in params:
            raise ValueError("source-mismatch: 当前派生执行只支持文本输出，不接受 structured output 控制")
        identity = sha256_jcs({
            "schema": "derived-provider-attempt:v1", "reference": asdict(request.reference),
            "execution_id": admission.execution_id, "checkpoint_ns": request.checkpoint_ns, "plan_id": request.plan.plan_id,
            "provider_profile": profile.identity(),
        })
        return _ProviderPreparation(
            admission=admission, checkpoint_ns=request.checkpoint_ns,
            creation_key=f"provider-create:{identity}", seal_key=f"provider-seal:{identity}",
            provider_version=profile.identity(), target_format=target_format, wire_profile=profile, wire_params=params,
            include_structured_output=False, bound_model=bound_model, response_format=None, allow_provider_reprojection=False,
            capacity=ModelCapacity(max_input_tokens=(request.model.profile or {}).get("max_input_tokens"),
                                   context_window=(request.model.profile or {}).get("context_window")),
        )

    async def _aprepare_derived(self, request: DerivedModelRequest) -> _PreparedModelCall:
        context = self._derived_preparation(request)
        activation = request.activation_snapshot
        callback_id = request.model_call_id
        UUID(callback_id)
        def prepare() -> dict[str, object]:
            frozen = self._checkpointer.freeze_context_for_provider(
                self._session_id, self._thread_id, turn_id=None, execution_id=context.admission.execution_id,
                plan_creation_idempotency_key=context.creation_key, seal_idempotency_key=context.seal_key,
                frozen_plan=request.plan, context_preparation=request.preparation,
                request_only_content=request.request_only_content,
                provider_version=context.provider_version, wire_profile=context.wire_profile, wire_params=context.wire_params,
                model_capacity=context.capacity, target_format=context.target_format, checkpoint_ns=context.checkpoint_ns,
                model_call_id=callback_id, activation_snapshot=activation,
            )
            require_provider_capabilities(request.model, frozen.request.semantic_messages
                                          if isinstance(frozen, PreparedProviderContext) else frozen["messages"])
            return self._checkpointer.seal_context_for_provider(
                frozen, callback_model_call_id=callback_id, checkpoint_ns=context.checkpoint_ns,
            )

        prepared = await run_sync_shielded(prepare)
        snapshot = prepared["snapshot"]
        if (snapshot.session_id, snapshot.thread_id, snapshot.execution_id, snapshot.turn_id, snapshot.plan_id) != (
            self._session_id, self._thread_id, context.admission.execution_id, None, request.plan.plan_id,
        ):
            raise ValueError("sealed-assembly binding violation: 派生请求 receipt 归属冲突")
        return cast(_PreparedModelCall, {**prepared, "bound_model": context.bound_model, "response_format": None,
                                       "callback_model_call_id": callback_id, "model_name": context.wire_profile.model})

    def _frozen_preparation(
        self, context: _ProviderPreparation,
    ) -> tuple[ContextPlanRegistration | None, ResourceActivationSnapshotRef]:
        if self._resource_activation is None:
            raise RuntimeError("resource-activation-unavailable: inspection graph 不得执行模型调用")
        admission = context.admission
        sealed = self._checkpointer.find_sealed_context_for_provider_creation(
            admission.session_id, admission.thread_id,
            plan_creation_idempotency_key=context.creation_key,
            checkpoint_ns=context.checkpoint_ns,
        )
        if sealed is not None:
            snapshot = sealed.sealed_snapshot
            if snapshot is None:
                raise ValueError("source-mismatch: sealed provider registration 缺少 snapshot")
            frozen = self._checkpointer.load_sealed_resource_activation(
                admission.session_id, thread_id=admission.thread_id,
                assembly_id=snapshot.assembly_id,
                plan_hash=snapshot.plan_hash, request_hash=snapshot.request_hash,
                checkpoint_ns=context.checkpoint_ns,
            )
            return sealed, frozen
        return None, require_current_execution_resource_activation(admission)

    def _prepare(self, request: ModelRequest[Any]) -> _PreparedModelCall:
        # 同步调用复用唯一异步准入链；不能在运行中的事件循环里等待 gate。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._aprepare(request))
        raise RuntimeError("同步 model-call preparation 不能阻塞运行中的事件循环")

    async def _settle_boundary(
        self, request: ModelRequest[Any], context: _ProviderPreparation,
    ) -> tuple[ToolProtocolBoundaryConflict | None, asyncio.CancelledError | None]:
        """排空 canonical worker；由持 gate 的调用者结算后传播预期拒绝或取消。"""
        worker = asyncio.create_task(asyncio.to_thread(
            self._checkpointer.prepare_model_call_boundary,
            context.admission.session_id, context.admission.thread_id,
            turn_id=context.admission.turn_id, execution_id=context.admission.execution_id,
            messages=request.messages, checkpoint_ns=context.checkpoint_ns,
            validated_predecessors={},
        ))

        async def settle() -> None:
            await worker

        cancelled = None
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            cancelled = error
        finally:
            await run_cleanup_shielded(settle)
        task = asyncio.current_task()
        if cancelled is None and task is not None and task.cancelling():
            cancelled = asyncio.CancelledError()
        conflict, _predecessor = worker.result()
        return conflict, cancelled

    async def _aprepare(self, request: ModelRequest[Any]) -> _PreparedModelCall:
        state = self._graph_compaction.get()
        adopted = None if state is None else state.messages
        if adopted is not None:
            request = request.override(messages=list(adopted))
        context = self._request_preparation(request)
        activation = self._resource_activation
        if activation is None:
            raise RuntimeError("resource-activation-unavailable: inspection graph 不得执行模型调用")
        callback_id = str(UUID(create_uuid_hex(namespace=f"model_call/{context.admission.session_id}/{context.admission.thread_id}")))
        async with self._context_read_admission():
            read_worker = asyncio.create_task(asyncio.to_thread(self._frozen_preparation, context))

            async def settle_read() -> None:
                await read_worker

            try:
                sealed, frozen = await asyncio.shield(read_worker)
            finally:
                # to_thread 继承本次 ContextVar owner；取消后仍须等真实读取
                # 结束再退出 read gate，不能让删除提前隔离 worker 的目录。
                await run_cleanup_shielded(settle_read)
        pending = None
        if sealed is None:
            preflight_id = "provider-boundary:" + sha256_jcs({
                "schema": "provider-boundary-input:v2",
                "creation_key": context.creation_key,
                "request_messages": [message.model_dump(mode="json") for message in request.messages],
            })
            async with self._context_control_admission(
                preflight_id,
                {"kind": "model_call_boundary", "execution_id": context.admission.execution_id,
                 "checkpoint_ns": context.checkpoint_ns},
                context.creation_key,
            ):
                conflict, cancelled = await self._settle_boundary(request, context)
            if cancelled is not None:
                raise cancelled
            if conflict is not None:
                raise conflict
            # 来源等待不持 Session gate；只有完整 tool group 才能进入冻结边界。
            pending = await activation.capture_model_call(
                parent=frozen, model_call_id=callback_id, checkpoint_ns=context.checkpoint_ns,
            )
        conflict, cancelled = None, None
        result = None
        async with self._context_control_admission(
            context.creation_key,
            {"kind": "tool_set", "execution_id": context.admission.execution_id,
             "checkpoint_ns": context.checkpoint_ns, "seal_key": context.seal_key},
            context.seal_key,
        ):
            if sealed is None:
                if pending is None:
                    raise RuntimeError("resource-activation-unavailable: model-call capture 缺失")
                # 等待来源时释放过 gate，提交 snapshot 前必须先核最新 canonical 边界。
                conflict, cancelled = await self._settle_boundary(request, context)
                if conflict is None and cancelled is None:
                    frozen = await activation.commit_model_call(pending)
            if conflict is None and cancelled is None:
                prepare_worker = asyncio.create_task(asyncio.to_thread(
                    self._complete_preparation, request, context, sealed, frozen, callback_id,
                    allow_compaction=adopted is None,
                ))

                async def settle_preparation() -> None:
                    await prepare_worker

                try:
                    await asyncio.shield(prepare_worker)
                except asyncio.CancelledError as error:
                    cancelled = error
                finally:
                    # 持原 gate/lease 直到真实提交线程结算。
                    await run_cleanup_shielded(settle_preparation)
                task = asyncio.current_task()
                if cancelled is None and task is not None and task.cancelling():
                    cancelled = asyncio.CancelledError()
                if cancelled is None:
                    result = prepare_worker.result()
        if cancelled is not None:
            raise cancelled
        if conflict is not None:
            raise conflict
        if isinstance(result, _CompactionPreparation):
            return await self._resolve_compaction(request, context, frozen, callback_id, result)
        if result is not None:
            if "compaction_target" in result:
                try:
                    handle = await self._request_compaction(
                        request, context, foreground=False,
                        target_constraint=result["compaction_target"],
                    )
                    if handle is not None:
                        self._compaction_runtime.note_background(MutationIntentOwner(self._session_id, self._thread_id), handle.task_id)
                except Exception as error:  # noqa: BLE001 — 后台受理失败只记录原故障，不否定已可发送的前台请求。
                    report_failure(error, detail="后台压缩受理失败", diagnostic_logger=logger,
                                   extra={"session_id": self._session_id, "thread_id": self._thread_id})
            return result
        raise RuntimeError("model-call preparation 未产生结果")

    @staticmethod
    def _compaction_target(
        budget: ContextBudget, policy: CompactionPolicy, retained: ContextBudget | None = None,
    ) -> dict[str, object]:
        # 提前准备的学习值不改变摘要目标，不能因此重开同范围的任务。
        return {"capacity": budget.capacity.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json", exclude={"prepare_fraction"}),
                "retained_input_tokens": None if retained is None else retained.estimate.input_tokens}

    async def _request_compaction(
        self, request: ModelRequest[Any], context: _ProviderPreparation, *, foreground: bool,
        predecessor: FrozenCompactionRange | None = None,
        target_constraint: Mapping[str, object] | None = None,
        deadline: datetime | None = None,
    ):
        if self._compaction_runtime is None or self._agent_id is None or self._compaction_model_recipe is None:
            raise RuntimeError("compaction-model-unavailable: 执行装配未冻结实际模型的摘要配置")
        return await self._compaction_runtime.request(
            MutationIntentOwner(self._session_id, self._thread_id), agent_id=self._agent_id,
            checkpoint_ns=context.checkpoint_ns, foreground=foreground,
            model_recipe=self._compaction_model_recipe(request.model),
            predecessor_candidate_id=None if predecessor is None else predecessor.candidate_id,
            target_constraint=target_constraint,
            deadline=deadline,
        )

    async def _resolve_compaction(
        self, request: ModelRequest[Any], context: _ProviderPreparation,
        activation: ResourceActivationSnapshotRef, callback_id: str, work: _CompactionPreparation,
    ) -> _PreparedModelCall:
        from app.runtime.context_compaction import compaction_deadline

        runtime = self._compaction_runtime
        if runtime is None:
            raise RuntimeError("compaction-runtime-unavailable: 压缩裁决缺少原执行端口")
        owner = MutationIntentOwner(self._session_id, self._thread_id)
        generation = work.generation
        feedback_task = None
        deadline = compaction_deadline(None)
        # 最多先结清旧 S1，再生成一次 S2；在途旧任务不能令缩窗行为与 ready 起步不同。
        for preparation_attempt in range(2):
            if work.candidate is None or not work.sufficient:
                waiting_task = runtime.waiting_background(owner) if work.reached_apply and work.policy.apply_policy == "on_demand" else None
                # 模型和队列等待不持 Session gate 或 SQLite 写锁。
                handle = await self._request_compaction(
                    request, context, foreground=True, predecessor=work.candidate,
                    target_constraint=self._compaction_target(work.budget, work.policy, work.retained_budget),
                    deadline=deadline,
                )
                if handle is None:
                    raise ContextCapacityError(work.budget, apply_fraction=work.policy.apply_fraction)
                completed, _summary = await runtime.wait(handle, deadline=deadline)
                if work.manual_requested:
                    await runtime.request_application(handle)
                if waiting_task == handle.task_id:
                    feedback_task = waiting_task
            else:
                completed = work.candidate
            state = self._graph_compaction.get()
            if state is None:
                raise RuntimeError("compaction-graph-scope-required: 采用必须属于当前图节点")
            checkpoint_id = request.runtime.execution_info.checkpoint_id
            async with self._context_control_admission(
                context.creation_key, {"kind": "tool_set", "execution_id": context.admission.execution_id,
                "checkpoint_ns": context.checkpoint_ns, "seal_key": context.seal_key}, context.seal_key,
            ):
                # 释放过 gate 后，统一重评一次来源／选集与完整候选；等待不保存旧模拟裁决。
                refreshed = await run_sync_shielded(
                    self._complete_preparation, request, context, None, activation, callback_id,
                )
                if not isinstance(refreshed, _CompactionPreparation):
                    return refreshed
                work = refreshed
                if work.candidate is None:
                    raise RuntimeError("compaction-result-unavailable: 完整任务结清后没有适用候选")
                if not work.sufficient:
                    if preparation_attempt == 0 and completed.predecessor is None:
                        continue
                    raise ContextCapacityError(work.budget, apply_fraction=work.policy.apply_fraction)
                if work.budget.input_fraction is not None and work.budget.input_fraction > work.policy.target_fraction:
                    logger.info("压缩候选未达到软目标，完整请求仍满足应用阈值: input_fraction=%s target_fraction=%s candidate=%s",
                                work.budget.input_fraction, work.policy.target_fraction, work.candidate.candidate_id)
                adopted = await self._checkpointer.aadopt_candidate(
                    build_checkpoint_config(self._thread_id, checkpoint_ns=context.checkpoint_ns, checkpoint_id=checkpoint_id),
                    ApplyCompactionCandidateIntent(work.candidate, work.prepared.plan.active_view_id),
                )
                messages = adopted.checkpoint["channel_values"]["messages"]
                state.messages = tuple(messages)
                result = await run_sync_shielded(
                    self._complete_preparation, request.override(messages=messages), context, None, activation, callback_id,
                    allow_compaction=False,
                )
            break
        if isinstance(result, _CompactionPreparation):
            raise TypeError("compaction-repeat-forbidden: 一次请求只能采用一次完整候选")
        if feedback_task is not None:
            runtime.record_generation_wait(owner, generation, feedback_task)
        return result

    def _complete_preparation(
        self, request: ModelRequest[Any], context: _ProviderPreparation,
        sealed: ContextPlanRegistration | None, activation: ResourceActivationSnapshotRef,
        callback_id: str, *, allow_compaction: bool = True,
    ) -> _PreparedModelCall | _CompactionPreparation:
        admission = context.admission
        session_id, thread_id, turn_id = admission.session_id, admission.thread_id, admission.turn_id
        checkpoint_ns = context.checkpoint_ns
        include_structured_output = context.include_structured_output
        original = None if sealed is None else sealed.sealed_snapshot
        if sealed is not None and (original is None or original.model_call_id is None):
            raise ValueError("source-mismatch: sealed provider registration 缺少 snapshot")
        sealed_model_call_id = callback_id if original is None else original.model_call_id
        if sealed_model_call_id is None:
            raise ValueError("source-mismatch: sealed provider registration 缺少 model-call identity")
        frozen = self._checkpointer.freeze_context_for_provider(
            session_id, thread_id,
            turn_id=turn_id, execution_id=admission.execution_id,
            plan_creation_idempotency_key=context.creation_key,
            seal_idempotency_key=context.seal_key,
            request_messages=request.messages,
            context_source_manager=self._context_source_manager,
            tool_set_supplier=lambda: self._tool_set_producer.snapshot(
                include_structured_output,
            ),
            extension_policy_resolver=self._extension_policy_resolver,
            provider_version=context.provider_version,
            wire_profile=context.wire_profile, wire_params=context.wire_params,
            model_capacity=context.capacity,
            target_format=context.target_format, checkpoint_ns=checkpoint_ns,
            model_call_id=sealed_model_call_id,
            activation_snapshot=activation,
            sealed_context=sealed,
            allow_provider_reprojection=context.allow_provider_reprojection,
            source_message_grouping=self._source_message_grouping_supplier() if sealed is None else "aggregate",
        )
        background_target = None
        enabled = False
        generation = None
        if isinstance(frozen, PreparedProviderContext):
            if frozen.request is None:
                raise RuntimeError("provider-preparation-missing: 主模型缺少完整预算投影")
            budget = frozen.request.budget(context.capacity)
            if self._compaction_runtime is not None:
                base_policy, enabled = self._compaction_runtime.policy()
                generation = sha256_jcs({"provider": context.provider_version,
                    "capacity": budget.capacity.model_dump(mode="json"), "policy": base_policy.model_dump(mode="json")})
                policy = self._compaction_runtime.effective_policy(MutationIntentOwner(session_id, thread_id), generation, base_policy)
                if not allow_compaction and enabled and budget.input_fraction is not None and budget.input_fraction >= policy.apply_fraction:
                    raise ContextCapacityError(budget, apply_fraction=policy.apply_fraction)
            if allow_compaction and self._compaction_runtime is not None:
                decision = budget.decision(policy, auto_summarize=enabled)
                state = self._graph_compaction.get()
                if enabled and state is not None and state.overflow_recovery:
                    decision = "compact"
                ready = self._compaction_runtime.ready(
                    MutationIntentOwner(session_id, thread_id), checkpoint_ns=checkpoint_ns,
                    include_automatic=enabled and (decision in {"prepare", "compact"} or policy.apply_policy == "next_request"),
                )
                replacement = None
                simulated = None
                if ready is not None:
                    candidate, summary = ready.frozen, ready.result
                    replacement = frozen.simulate_candidate(candidate, summary)
                    simulated = replacement.budget(context.capacity)
                manual_requested = ready is not None and ready.apply_on_next_request
                if decision == "compact" or ready is not None and (manual_requested or enabled and policy.apply_policy == "next_request"):
                    if frozen.project_retained_input is None:
                        raise RuntimeError("provider-preparation-missing: 主模型缺少强制保留输入投影")
                    retained = frozen.project_retained_input().budget(context.capacity)
                    if retained.exceeds_capacity or retained.input_fraction is not None and retained.input_fraction >= policy.apply_fraction:
                        # 连历史全部省去都不能适配时，生成摘要也无助于本次请求。
                        raise ContextCapacityError(retained, apply_fraction=policy.apply_fraction)
                    if ready is not None:
                        require_provider_capabilities(request.model, replacement.semantic_messages)
                        sufficient = not simulated.exceeds_capacity and (
                            simulated.input_fraction is None or simulated.input_fraction < policy.apply_fraction
                        )
                        return _CompactionPreparation(
                            frozen, simulated, candidate, policy, generation, decision == "compact",
                            sufficient=sufficient, manual_requested=manual_requested, retained_budget=retained,
                        )
                    require_provider_capabilities(request.model, frozen.request.semantic_messages)
                    return _CompactionPreparation(frozen, budget, None, policy, generation, True, retained_budget=retained)
                if decision == "prepare" and (simulated is None or simulated.decision(policy, auto_summarize=True) in {"prepare", "compact"}):
                    # 以假设采用 S1 后的完整请求判断是否需要 S2，不能被尚未采用的大 view 反复触发。
                    if frozen.project_retained_input is None:
                        raise RuntimeError("provider-preparation-missing: 主模型缺少强制保留输入投影")
                    background_target = self._compaction_target(budget, policy, frozen.project_retained_input().budget(context.capacity))
            require_provider_capabilities(request.model, frozen.request.semantic_messages)
        else:
            # 重用原封存正文仍须核实际模型能力，不能因跳过新计划而绕过检查。
            require_provider_capabilities(request.model, frozen["messages"])
        prepared = dict(self._checkpointer.seal_context_for_provider(
            frozen, callback_model_call_id=callback_id, checkpoint_ns=checkpoint_ns,
        ))
        if background_target is not None:
            prepared["compaction_target"] = background_target
        prepared["bound_model"] = context.bound_model
        prepared["response_format"] = context.response_format
        prepared["callback_model_call_id"] = callback_id
        prepared["activation_snapshot"] = activation
        prepared["include_structured_output"] = include_structured_output
        # prepare 返回同一已提交、已验证的 assembly，禁止重新读取另一份
        # snapshot 后把它和本次消息/工具投影拼在一起。
        snapshot = prepared.get("snapshot")
        if not isinstance(snapshot, ContextAssemblySnapshot):
            raise TypeError("source-mismatch: Saver 未返回已提交 ContextAssemblySnapshot")
        if (
            snapshot.session_id != session_id
            or snapshot.thread_id != thread_id
            or snapshot.execution_id != admission.execution_id
            or snapshot.turn_id != turn_id
            or snapshot.assembly_id != prepared["assembly_id"]
            or snapshot.as_sealed_plan() != prepared["plan"]
        ):
            raise ValueError("source-mismatch: Saver assembly 与当前 prepared owner/plan 不一致")
        prepared["snapshot"] = snapshot
        if (enabled and generation is not None and isinstance(frozen, PreparedProviderContext)
                and self._agent_id is not None and self._compaction_model_recipe is not None):
            # 预算、策略和水位均来自同次冻结，不能混入 seal 期间的新配置或跳过稳定内容。
            self._compaction_runtime.seed_observation(
                MutationIntentOwner(session_id, thread_id), budget, frozen.item_sequence, generation,
                agent_id=self._agent_id, model_recipe=self._compaction_model_recipe(request.model),
                wire_profile=context.wire_profile,
                reassess_sources=partial(frozen.reassess_sources, snapshot) if frozen.reassess_sources is not None else None,
            )
        if self._extension_call_scope is not None:
            # 冻结正文的读取仍在原 Session gate 内；短期连接占用在真实发送前进入。
            prepared["extension_call_scope"] = self._extension_call_scope(activation, callback_id, checkpoint_ns)
        if _target_format(request.model) == "responses":
            native = prepared.get("native_projection")
            if not isinstance(native, Mapping):
                raise TypeError("itemized context 缺少本次 Saver prepare 的 native projection")
            prepared["losses"] = tuple(native["losses"])
        logger.warning(
            "[itemized-context] provider prepare completed: session_id=%s turn_id=%s checkpoint_ns=%s assembly_id=%s",
            session_id,
            turn_id,
            checkpoint_ns,
            prepared.get("assembly_id"),
        )
        return cast(_PreparedModelCall, prepared)

    def _prepare_projection(
        self,
        request: ModelRequest[Any],
    ) -> tuple[_PreparedModelCall, list[BaseMessage], list[dict[str, object]]]:
        """执行 provider projection，校验 messages/tools 结构并记录结果与能力损失。"""
        return self._project_prepared(request, self._prepare(request))

    def _project_prepared(
        self, request: ModelRequest[Any] | DerivedModelRequest, prepared: _PreparedModelCall,
    ) -> tuple[_PreparedModelCall, list[BaseMessage], list[dict[str, object]]]:
        messages = prepared.get("messages")
        tools = prepared.get("tools")
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise TypeError("itemized provider projection 返回结构非法")
        logger.warning(
            "[itemized-context] provider projection result: message_types=%s message_ids=%s",
            [type(message).__name__ for message in messages],
            [getattr(message, "id", None) for message in messages],
        )
        losses = prepared.get("losses", ())
        if losses:
            logger.warning(
                "itemized provider projection capability loss: session=%s turn=%s losses=%s",
                self._session_id,
                require_current_execution_admission(langgraph_thread_id=self._thread_id).job_id,
                losses,
            )
        return prepared, messages, tools

    def _response(self, output: AIMessage, prepared: _PreparedModelCall) -> ModelResponse[Any]:
        if self._model_name:
            output.name = self._model_name
        decode = self._decode_output
        if decode is None:
            raise RuntimeError("Graph model-call 缺少输出 decoder")
        handled = decode(output, prepared["response_format"])
        return ModelResponse(result=handled["messages"], structured_response=handled.get("structured_response"))

    def _invoke(self, prepared: _PreparedModelCall, messages: list[BaseMessage], tools: list[dict[str, object]]) -> ModelResponse[Any]:
        output = self._dispatch_model(prepared, tools).invoke(messages)
        return self._response(output, prepared)

    async def _ainvoke(
        self, prepared: _PreparedModelCall, messages: list[BaseMessage], tools: list[dict[str, object]], *,
        response_decoder: Callable[[AIMessage, _PreparedModelCall], ModelResponse[Any]] | None = None,
        config: Mapping[str, object] | None = None,
        provider_wait: Callable[[bool], None] | None = None,
    ) -> ModelResponse[Any]:
        model = self._dispatch_model(prepared, tools)
        if provider_wait is not None:
            provider_wait(True)
        try:
            output = await model.ainvoke(messages, config=config)
        finally:
            if provider_wait is not None:
                provider_wait(False)
        return (self._response if response_decoder is None else response_decoder)(output, prepared)

    @staticmethod
    def _derived_response(output: AIMessage, _prepared: _PreparedModelCall) -> ModelResponse[Any]:
        return ModelResponse(result=[output])

    @staticmethod
    def _dispatch_model(prepared: _PreparedModelCall, tools: list[dict[str, object]]) -> Runnable[Any, AIMessage]:
        # 完整替换 sealed ToolSet；空集合在 wire 中省略 tools，不能额外发送 []。
        binding = prepared["bound_model"]
        if not isinstance(binding, RunnableBinding):
            raise TypeError("model-call execution 缺少可冻结的模型 binding")
        options = {key: value for key, value in binding.kwargs.items() if key != "tools"}
        if tools:
            options["tools"] = tools
        return binding.model_copy(update={"kwargs": options})

    def execute_derived(self, request: DerivedModelRequest) -> DerivedModelResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aexecute_derived(request))
        raise RuntimeError("同步派生请求不能阻塞运行中的事件循环")

    async def aexecute_derived(
        self, request: DerivedModelRequest, *,
        publish: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> DerivedModelResult:
        # 使用独立 callback adapter，只登记本次 model-call；不触发聊天 finalization。
        from app.services.orchestration.execution_step.model_call import (
            StepModelCallAdapter,
        )

        if get_current_model_delta_sink() is not None:
            raise RuntimeError("派生请求不得继承前台 model delta sink，请在独立任务上下文执行")
        prepared = None
        completed = False
        try:
            # 取消后仍接住 worker 生成的唯一 receipt，才能结算并释放 pending handle。
            async def prepare_call() -> None:
                nonlocal prepared
                prepared = await self._aprepare_derived(request)

            await run_cleanup_shielded(prepare_call)
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise asyncio.CancelledError()
            prepared, messages, tools = self._project_prepared(request, cast(_PreparedModelCall, prepared))
            adapter = StepModelCallAdapter(
                checkpointer=self._checkpointer, session_id=self._session_id, thread_id=self._thread_id,
                job_id=request.admission.job_id, checkpoint_ns=request.checkpoint_ns,
            )
            call_id = prepared["callback_model_call_id"]
            await run_cleanup_shielded(lambda: adapter.register(call_id, request.model_call_attempt, prepared["snapshot"].provider_version))
            if publish is not None:
                await publish("llm_request", {
                    "model": prepared["model_name"],
                    "timestamp": int(datetime.now(UTC).timestamp() * 1000),
                })
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise asyncio.CancelledError()
            log_request = ModelCallLogRequest(
                request.admission, request.model, messages, purpose=request.model.invocation_purpose, derived_reference=request.reference,
            )
            logs = LLMLoggingMiddleware(sessions_dir=self._checkpointer._storage.storage.sessions_dir)
            with sealed_assembly_dispatch_scope(
                prepared["dispatch_ref"], prepared["snapshot"], resource_refs=prepared["resource_refs"],
                native_projection=prepared.get("native_projection"),
            ):
                response = await logs.alog_call(log_request, lambda: self._ainvoke(
                    prepared, messages, tools, response_decoder=self._derived_response,
                    config={"callbacks": [], "tags": [], "metadata": {}}, provider_wait=request.provider_wait,
                ))
            if not isinstance(response, ModelResponse) or len(response.result) != 1 or not isinstance(response.result[0], AIMessage):
                raise ValueError("派生模型必须返回唯一 assistant 消息")
            output = response.result[0]
            if output.invalid_tool_calls:
                raise ValueError("派生模型返回非法工具参数")
            response = _bind_response_model_call(response, model_call_id=call_id)
            output = response.result[0]
            items = LangChainMessageCodec().items_for_message(
                output, item_sequence=1, message_id=output.id or f"derived:{call_id}",
                turn_id=None, execution_id=request.admission.execution_id,
                timestamp=datetime.now(UTC).isoformat(), model_call_id=call_id,
            )

            async def commit() -> None:
                nonlocal completed
                await run_sync_shielded(
                    self._checkpointer._storage.executions.append_items,
                    self._session_id, self._thread_id, items,
                    execution_id=request.admission.execution_id, checkpoint_ns=request.checkpoint_ns,
                )
                await adapter.update_outcome(call_id, "completed")
                completed = True

            await run_cleanup_shielded(commit)
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise asyncio.CancelledError()
            stored = await run_sync_shielded(
                self._checkpointer.read_canonical_items, self._session_id, self._thread_id,
                checkpoint_ns=request.checkpoint_ns, item_ids=tuple(item.item_id for item in items),
            )
            return DerivedModelResult(response, prepared["dispatch_ref"], tuple(
                ContextRef.canonical_item(item, session_id=self._session_id, thread_id=self._thread_id)
                for item in stored if item.semantic_kind == "assistant_output"
            ))
        except BaseException as error:
            if not completed:
                outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
                try:
                    if prepared is not None:
                        await run_cleanup_shielded(lambda: run_sync_shielded(
                            self._checkpointer.update_model_call_outcome, self._session_id,
                            model_call_id=prepared["callback_model_call_id"], thread_id=self._thread_id,
                            outcome=outcome, dispatch_state="failed", checkpoint_ns=request.checkpoint_ns,
                        ))
                except Exception as settlement_error:  # noqa: BLE001 — 保留原故障并透明附加结算失败。
                    error.add_note(f"派生执行结算失败: {type(settlement_error).__name__}: {settlement_error}")
            if prepared is not None:
                self._checkpointer.discard_prepared_context_for_dispatch(
                    self._session_id, self._thread_id, execution_id=request.admission.execution_id,
                    assembly_id=prepared["assembly_id"], checkpoint_ns=request.checkpoint_ns,
                )
            raise

    def execute(
        self,
        request: ModelRequest[Any],
    ) -> ModelResponse[Any]:
        for call_id in LangChainMessageCodec().tool_result_call_ids(request.messages):
            observe_phase_sync(call_id, "canonical.tool_result.before_commit")
        for attempt in range(2):
            prepared, messages, tools = self._prepare_projection(request)
            try:
                with prepared.get("extension_call_scope", nullcontext()) as retain_calls, sealed_assembly_dispatch_scope(
                    prepared["dispatch_ref"], prepared["snapshot"],
                    resource_refs=prepared["resource_refs"],
                    native_projection=prepared.get("native_projection"),
                ):
                    response = self._invoke(prepared, messages, tools)
                    return _bind_response_model_call(
                        response, model_call_id=prepared["callback_model_call_id"], retain_calls=retain_calls,
                    )
            except Exception as error:
                if attempt or not self._recover_context_overflow(request, error):
                    raise

    def _recover_context_overflow(
        self, request: ModelRequest[Any], error: Exception,
    ) -> bool:
        from app.runtime.provider_errors import is_context_overflow

        state = self._graph_compaction.get()
        if state is None or state.messages is not None or state.overflow_recovery or self._compaction_runtime is None:
            return False
        if not is_context_overflow(error):
            return False
        _policy, enabled = self._compaction_runtime.policy()
        profile = request.model.profile or {}
        if not enabled or profile.get("max_input_tokens") is not None or profile.get("context_window") is not None:
            return False
        # 保留上游失败事实；同一图节点只允许一次全覆盖补压缩，不学习准备阈值。
        state.overflow_recovery = True
        return True

    async def aexecute(
        self,
        request: ModelRequest[Any],
    ) -> ModelResponse[Any]:
        # ToolMessage 可能先于外层 on_tool_end 抵达请求边界；在任何同步
        # preparation/rollout 锁之前观察同一真实结果，不能阻塞 backend loop。
        canonical_messages = request.messages
        for call_id in LangChainMessageCodec().tool_result_call_ids(canonical_messages):
            await observe_phase(call_id, "canonical.tool_result.before_commit")
        delta_sink = get_current_model_delta_sink()
        reconcile = getattr(delta_sink, "complete_tool_from_message", None)
        tool_results: list[tuple[ToolMessage, str]] = []
        if callable(reconcile):
            codec = LangChainMessageCodec()
            resolve_model_call = codec.tool_message_model_call_resolver(canonical_messages)
            for index, message in enumerate(canonical_messages):
                if not isinstance(message, ToolMessage):
                    continue
                emitting_model_call_id = resolve_model_call(index)
                if emitting_model_call_id is None:
                    raise RuntimeError("source-mismatch: ToolMessage reconciliation 缺少 emitting model-call identity")
                tool_results.append((message, emitting_model_call_id))
        # 真实 attempt 先提交唯一终态；随后 request carrier 只保留其独立投影身份。
        for message, emitting_model_call_id in tool_results:
            await cast(Callable[..., Awaitable[None]], reconcile)(message, model_call_id=emitting_model_call_id)
        for attempt in range(2):
            prepared, messages, tools = self._project_prepared(request, await self._aprepare(request))
            try:
                # 成功返回后仍保留 handle，供外层真实 model-start 事件绑定。
                with prepared.get("extension_call_scope", nullcontext()) as retain_calls, sealed_assembly_dispatch_scope(
                    prepared["dispatch_ref"], prepared["snapshot"],
                    resource_refs=prepared["resource_refs"],
                    native_projection=prepared.get("native_projection"),
                ):
                    response = await self._ainvoke(prepared, messages, tools)
                    return _bind_response_model_call(
                        response, model_call_id=prepared["callback_model_call_id"], retain_calls=retain_calls,
                    )
            except Exception as error:
                if attempt or not self._recover_context_overflow(request, error):
                    raise


__all__ = ["DerivedModelRequest", "DerivedModelResult", "ModelCallExecution"]
