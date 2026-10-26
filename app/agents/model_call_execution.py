"""唯一 model-call 执行入口：收敛 canonical、准备封存，然后直接发送。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast, overload
from uuid import UUID

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain.agents.structured_output import ResponseFormat, ToolStrategy
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableBinding

from app.agents.model_capability_routing import current_selected_provider
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
from app.core.execution_admission_context import require_current_execution_admission
from app.core.identifier import create_uuid_hex
from app.core.model_delta_context import get_current_model_delta_sink
from app.core.model_request_context import current_summary_candidate
from app.core.phase_observer import observe_phase, observe_phase_sync
from app.core.shielded_cleanup import run_cleanup_shielded
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import matches_tool_call_id
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.schemas.internal_v2.session_context import SessionContextResourceDTO
from app.services.infrastructure.rollout_context.assembly.plans.types import (
    ContextPlanRegistration,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    ToolProtocolBoundaryConflict,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from app.services.orchestration.resource_activation.contracts import (
    require_current_execution_resource_activation,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.checkpoint.saver import (
        RolloutCheckpointSaver,
    )

logger = logging.getLogger(__name__)


def _canonical_messages(request: ModelRequest[Any]) -> Sequence[BaseMessage]:
    candidate = current_summary_candidate()
    return candidate.canonical_messages if candidate is not None else request.messages


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
            "summary_candidate": (
                scope.identity() if (scope := current_summary_candidate()) is not None else None
            ),
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
def _bind_response_model_call(response: ModelResponse[Any], *, model_call_id: str) -> ModelResponse[Any]: ...


@overload
def _bind_response_model_call(response: ExtendedModelResponse[Any], *, model_call_id: str) -> ExtendedModelResponse[Any]: ...


def _bind_response_model_call(
    response: ModelResponse[Any] | ExtendedModelResponse[Any], *, model_call_id: str,
) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
    """在本次 dispatch 返回边界保留真实 callback 身份，不从 vendor ID 推导。"""
    model_response = response.model_response if isinstance(response, ExtendedModelResponse) else response
    codec = LangChainMessageCodec()
    identity = ItemProjectionIdentity(model_call_id=model_call_id)
    messages = []
    for message in model_response.result:
        if isinstance(message, (AIMessage, ToolMessage)):
            existing = codec.model_call_id(message)
            if existing is not None and existing != model_call_id:
                raise ValueError(
                    "source-mismatch: dispatch response 的 model_call_id 冲突: "
                    f"message_id={message.id!r}, expected={model_call_id!r}, actual={existing!r}"
                )
            if isinstance(message, ToolMessage):
                matches = [
                    call
                    for assistant in messages if isinstance(assistant, AIMessage)
                    for call in assistant.tool_calls
                    if isinstance(call["id"], str)
                    and matches_tool_call_id(identity, call["id"], message.tool_call_id)
                ]
                if len(matches) != 1:
                    raise ValueError(
                        "source-mismatch: dispatch ToolMessage 缺少唯一 emitting assistant: "
                        f"tool_call_id={message.tool_call_id!r}"
                    )
            message = message.model_copy(update={
                "response_metadata": {**message.response_metadata, "model_call_id": model_call_id},
            })
        messages.append(message)
    bound = replace(model_response, result=messages)
    return replace(response, model_response=bound) if isinstance(response, ExtendedModelResponse) else bound


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


class ModelCallExecution:
    """按真实 attempt 完成准备并直接消费 Saver 的封存 payload。"""

    def __init__(
        self, *, checkpointer: object, session_id: str, thread_id: str,
        context_source_manager: ContextSourceManager,
        tool_set_producer: RegisteredToolSetProducer,
        resource_activation: ResourceActivationCoordinator | None,
        source_message_grouping_supplier: Callable[[], Literal["aggregate", "separate"]] = lambda: "aggregate",
        extension_policy_resolver: Callable[[Sequence[ToolMetadata]], Mapping[str, ToolCapabilityPolicy]] | None = None,
        context_read_admission: Callable[[], AbstractAsyncContextManager[None]],
        context_control_admission: Callable[[str, Mapping[str, object], str], AbstractAsyncContextManager[None]],
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
        self._graph_model_binding: Callable[[ModelRequest[Any]], tuple[Runnable[Any, AIMessage], ResponseFormat[Any] | None]] | None = None
        self._decode_output: Callable[..., Mapping[str, Any]] | None = None
        self._model_name: str | None = None

    def bind_model(self, *, bind_model, decode_output, name, structured_tools) -> None:
        """图适配器只注入一次框架的真实绑定、解析和结构化工具注册表。"""
        if self._graph_model_binding is not None:
            raise RuntimeError("model-call execution 已绑定模型")
        self._tool_set_producer.freeze_structured_output_tools(structured_tools)
        self._graph_model_binding = bind_model
        self._decode_output = decode_output
        self._model_name = name

    def _request_preparation(self, request: ModelRequest[Any]) -> _ProviderPreparation:
        if request.system_message is not None:
            raise RuntimeError("instruction-producer-unregistered: 最终请求出现未登记 system prompt")
        session_id = self._session_id
        thread_id = _runtime_thread_id(request)
        if thread_id != self._thread_id:
            raise RuntimeError(
                "itemized context runtime thread 与 Agent owner 不一致: "
                f"expected={self._thread_id!r}, actual={thread_id!r}"
            )
        checkpoint_ns = _runtime_checkpoint_ns(request)
        admission = require_current_execution_admission(
            langgraph_thread_id=thread_id
        )
        if admission.session_id != session_id or admission.thread_id != thread_id:
            raise RuntimeError("itemized context request 与当前 execution owner 不一致")
        turn_id = admission.turn_id
        prepare = getattr(self._checkpointer, "prepare_context_for_provider", None)
        if not callable(prepare):
            raise TypeError("itemized context 缺少必需的 Saver prepare 端口")
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
            # Agent dispatch 的真实发送入口是异步流式；封存与发送必须同一形态。
            wire_params = chat_wire_params(
                request.model, stop=stop, kwargs=options, stream=True,
            )
        # provider_version 只承载实际 wire profile 身份，不用 model name 冒充。
        provider_version = wire_profile.identity()
        allow_provider_reprojection = current_selected_provider() is request.model
        creation_key, seal_key = _request_idempotency_keys(
            request,
            session_id=session_id,
            turn_id=turn_id,
            checkpoint_ns=checkpoint_ns,
            execution_id=admission.execution_id,
            provider_profile=f"{target_format}:{provider_version}",
        )
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
        )

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
            messages=_canonical_messages(request), checkpoint_ns=context.checkpoint_ns,
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
            candidate = current_summary_candidate()
            preflight_id = "provider-boundary:" + sha256_jcs({
                "schema": "provider-boundary-input:v1",
                "creation_key": context.creation_key,
                "request_messages": [message.model_dump(mode="json") for message in request.messages],
                "canonical_messages": [message.model_dump(mode="json") for message in _canonical_messages(request)],
                "summary_candidate": candidate.identity() if candidate is not None else None,
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
                    return prepare_worker.result()
        if cancelled is not None:
            raise cancelled
        if conflict is not None:
            raise conflict
        raise RuntimeError("model-call preparation 未产生结果")

    def _complete_preparation(
        self, request: ModelRequest[Any], context: _ProviderPreparation,
        sealed: ContextPlanRegistration | None, activation: ResourceActivationSnapshotRef,
        callback_id: str,
    ) -> _PreparedModelCall:
        admission = context.admission
        session_id, thread_id, turn_id = admission.session_id, admission.thread_id, admission.turn_id
        checkpoint_ns = context.checkpoint_ns
        include_structured_output = context.include_structured_output
        candidate = current_summary_candidate()
        original = None if sealed is None else sealed.sealed_snapshot
        if sealed is not None and (original is None or original.model_call_id is None):
            raise ValueError("source-mismatch: sealed provider registration 缺少 snapshot")
        sealed_model_call_id = callback_id if original is None else original.model_call_id
        if sealed_model_call_id is None:
            raise ValueError("source-mismatch: sealed provider registration 缺少 model-call identity")
        prepared = self._checkpointer.prepare_context_for_provider(
            session_id, thread_id,
            turn_id=turn_id, execution_id=admission.execution_id,
            plan_creation_idempotency_key=context.creation_key,
            seal_idempotency_key=context.seal_key,
            request_messages=request.messages,
            context_source_manager=self._context_source_manager,
            tool_set_supplier=lambda: self._tool_set_producer.snapshot(
                include_structured_output,
                tools_enabled=candidate is None or candidate.tools_enabled,
            ),
            extension_policy_resolver=self._extension_policy_resolver,
            provider_version=context.provider_version,
            wire_profile=context.wire_profile, wire_params=context.wire_params,
            target_format=context.target_format, checkpoint_ns=checkpoint_ns,
            model_call_id=sealed_model_call_id,
            callback_model_call_id=callback_id,
            activation_snapshot=activation,
            summary_candidate=candidate,
            sealed_context=sealed,
            allow_provider_reprojection=context.allow_provider_reprojection,
            source_message_grouping=self._source_message_grouping_supplier() if sealed is None else "aggregate",
        )
        prepared = dict(prepared)
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
        self, request: ModelRequest[Any], prepared: _PreparedModelCall,
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
                require_current_execution_admission(
                    langgraph_thread_id=_runtime_thread_id(request)
                ).job_id,
                losses,
            )
        return prepared, messages, tools

    def _response(self, output: AIMessage, prepared: _PreparedModelCall) -> ModelResponse[Any]:
        if self._model_name:
            output.name = self._model_name
        decode = self._decode_output
        if decode is None:
            raise RuntimeError("model-call execution 尚未绑定输出解析器")
        handled = decode(output, prepared["response_format"])
        return ModelResponse(result=handled["messages"], structured_response=handled.get("structured_response"))

    def _invoke(self, prepared: _PreparedModelCall, messages: list[BaseMessage], tools: list[dict[str, object]]) -> ModelResponse[Any]:
        output = self._dispatch_model(prepared, tools).invoke(messages)
        return self._response(output, prepared)

    async def _ainvoke(self, prepared: _PreparedModelCall, messages: list[BaseMessage], tools: list[dict[str, object]]) -> ModelResponse[Any]:
        output = await self._dispatch_model(prepared, tools).ainvoke(messages)
        return self._response(output, prepared)

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

    def execute(
        self,
        request: ModelRequest[Any],
    ) -> ModelResponse[Any]:
        for call_id in LangChainMessageCodec().tool_result_call_ids(_canonical_messages(request)):
            observe_phase_sync(call_id, "canonical.tool_result.before_commit")
        prepared, messages, tools = self._prepare_projection(request)
        # LangChain 的 provider handler 返回时，外层 astream_events 可能尚未
        # 消费 on_chat_model_start。prepared handle 必须保留到该事件由
        # RolloutCheckpointSaver 绑定真实 model_call_id 后再移除，不能在
        # execution 的返回路径提前清理。
        with sealed_assembly_dispatch_scope(
            prepared["dispatch_ref"], prepared["snapshot"],
            resource_refs=prepared["resource_refs"],
            native_projection=prepared.get("native_projection"),
        ):
            response = self._invoke(prepared, messages, tools)
            return _bind_response_model_call(
                response, model_call_id=prepared["callback_model_call_id"],
            )

    async def aexecute(
        self,
        request: ModelRequest[Any],
    ) -> ModelResponse[Any]:
        # ToolMessage 可能先于外层 on_tool_end 抵达请求边界；在任何同步
        # preparation/rollout 锁之前观察同一真实结果，不能阻塞 backend loop。
        canonical_messages = _canonical_messages(request)
        for call_id in LangChainMessageCodec().tool_result_call_ids(canonical_messages):
            await observe_phase(call_id, "canonical.tool_result.before_commit")
        delta_sink = get_current_model_delta_sink()
        reconcile = getattr(delta_sink, "complete_tool_from_message", None)
        tool_results: list[tuple[ToolMessage, str]] = []
        if callable(reconcile):
            codec = LangChainMessageCodec()
            for index, message in enumerate(canonical_messages):
                if not isinstance(message, ToolMessage):
                    continue
                emitting_model_call_id = codec.tool_message_model_call_id(message, canonical_messages[:index])
                if emitting_model_call_id is None:
                    raise RuntimeError("source-mismatch: ToolMessage reconciliation 缺少 emitting model-call identity")
                tool_results.append((message, emitting_model_call_id))
        # 真实 attempt 先提交唯一终态；随后 request carrier 只保留其独立投影身份。
        for message, emitting_model_call_id in tool_results:
            await cast(Callable[..., Awaitable[None]], reconcile)(message, model_call_id=emitting_model_call_id)
        prepared, messages, tools = self._project_prepared(request, await self._aprepare(request))
        # 同步执行入口的相同生命周期约束：真实 model-start event 消费前不
        # 得删除 Saver-owned prepared handle。
        with sealed_assembly_dispatch_scope(
            prepared["dispatch_ref"], prepared["snapshot"],
            resource_refs=prepared["resource_refs"],
            native_projection=prepared.get("native_projection"),
        ):
            response = await self._ainvoke(prepared, messages, tools)
            return _bind_response_model_call(
                response, model_call_id=prepared["callback_model_call_id"],
            )


__all__ = ["ModelCallExecution"]
