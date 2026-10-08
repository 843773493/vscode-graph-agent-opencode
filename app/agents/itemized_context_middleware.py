"""把 Saver-owned itemized context 接到 LangChain model request 边界。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from inspect import getclosurevars
from typing import Any
from uuid import UUID

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse, StateT
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from app.agents.providers.wire_finalize import (
    ChatWireProfile,
    ResponsesWireProfile,
    chat_wire_params,
    chat_wire_profile,
    responses_wire_params,
    responses_wire_profile,
)
from app.agents.sealed_assembly_dispatch import sealed_assembly_dispatch_scope
from app.agents.tool_identity import tool_definition_name
from app.agents.tool_set_producer import RegisteredToolSetProducer
from app.core.execution_admission_context import require_current_execution_admission
from app.core.identifier import create_uuid_hex
from app.core.model_delta_context import get_current_model_delta_sink
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import matches_tool_call_id
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
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

logger = logging.getLogger(__name__)


def _runtime_thread_id(request: ModelRequest[Any]) -> str:
    runtime = request.runtime
    if runtime is None:
        raise RuntimeError("itemized context middleware 缺少 runtime")
    execution_info = runtime.execution_info
    thread_id = getattr(execution_info, "thread_id", None)
    if not isinstance(thread_id, str) or not thread_id:
        raise RuntimeError("itemized context middleware 缺少 runtime thread_id")
    return thread_id


def _runtime_checkpoint_ns(request: ModelRequest[Any]) -> str:
    runtime = request.runtime
    if runtime is None:
        raise RuntimeError("itemized context middleware 缺少 runtime")
    execution_info = runtime.execution_info
    checkpoint_ns = getattr(execution_info, "checkpoint_ns", "")
    if not isinstance(checkpoint_ns, str):
        raise TypeError("itemized context middleware checkpoint_ns 非法")
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
            "itemized context middleware 缺少 checkpoint/task/execution identity"
        )
    if type(attempt) is not int or attempt < 1:
        raise RuntimeError("itemized context middleware 缺少合法 node_attempt")
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
                    if matches_tool_call_id(identity, call["id"], message.tool_call_id)
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


class SealedAssemblyDispatchBridge(AgentMiddleware[StateT, Any, Any]):
    """用构建时的 SessionThread owner 转发 Saver sealed selection。"""

    def __init__(
        self, *, checkpointer: object, session_id: str, thread_id: str,
        context_source_manager: ContextSourceManager,
        tool_set_producer: RegisteredToolSetProducer,
        resource_activation: ResourceActivationCoordinator | None,
    ) -> None:
        if not session_id or not thread_id:
            raise ValueError("itemized context middleware 需要显式 SessionThread owner")
        self._checkpointer = checkpointer
        self._session_id = session_id
        self._thread_id = thread_id
        self._context_source_manager = context_source_manager
        self._tool_set_producer = tool_set_producer
        self._resource_activation = resource_activation
        self._graph_model_binding: Callable[[ModelRequest[Any]], tuple[object, object]] | None = None

    def bind_graph(self, graph: CompiledStateGraph) -> None:
        """在 Agent 适配边界读取实际图注册表和绑定函数，不修改框架闭包。

        LangChain 未公开该注册端口；访问集中在此处。图创建后工具已经注册，
        这里冻结同一批 schema，再使用同一 _get_bound_model 决定请求策略。
        """
        if self._graph_model_binding is not None:
            raise RuntimeError("itemized context bridge 已绑定 Agent 图")
        model_node = graph.builder.nodes["model"].runnable.func
        execute_model = getclosurevars(model_node).nonlocals["_execute_model_sync"]
        bind_model = getclosurevars(execute_model).nonlocals["_get_bound_model"]
        registry = getclosurevars(bind_model).nonlocals["structured_output_tools"]
        if not isinstance(registry, Mapping) or not callable(bind_model):
            raise TypeError("itemized context 缺少实际图结构化输出注册表/绑定端口")
        tools = {name: binding.tool for name, binding in registry.items()}
        if not all(isinstance(tool, BaseTool) for tool in tools.values()):
            raise TypeError("itemized context 图结构化输出注册表包含非法工具")
        self._tool_set_producer.freeze_structured_output_tools(tools)
        self._graph_model_binding = bind_model

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
        supports = getattr(self._checkpointer, "supports_itemized_context", None)
        if not callable(prepare) or not callable(supports):
            raise TypeError("itemized context 缺少必需的 Saver prepare/supports 端口")
        if not supports(session_id, thread_id, checkpoint_ns=checkpoint_ns):
            raise RuntimeError("itemized context 必须使用可用的 v2 Saver owner")
        if (
            (admission.execution_origin == "user_turn" and turn_id is None)
            or (admission.execution_origin == "internal" and turn_id is not None)
        ):
            raise RuntimeError("itemized context Turn 与 execution admission origin 不一致")
        target_format = _target_format(request.model)
        if self._graph_model_binding is None:
            raise RuntimeError("itemized context bridge 尚未绑定实际 Agent 图")
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
        )

    def _frozen_preparation(
        self, context: _ProviderPreparation,
    ) -> tuple[ContextAssemblySnapshot | None, ResourceActivationSnapshotRef]:
        if self._resource_activation is None:
            raise RuntimeError("resource-activation-unavailable: inspection graph 不得执行模型调用")
        admission = context.admission
        sealed = self._checkpointer.find_sealed_context_for_provider_creation(
            admission.session_id, admission.thread_id,
            plan_creation_idempotency_key=context.creation_key,
            checkpoint_ns=context.checkpoint_ns,
        )
        if sealed is not None:
            frozen = self._checkpointer.load_sealed_resource_activation(
                admission.session_id, thread_id=admission.thread_id,
                assembly_id=sealed.assembly_id,
                plan_hash=sealed.plan_hash, request_hash=sealed.request_hash,
                checkpoint_ns=context.checkpoint_ns,
            )
            return sealed, frozen
        return None, require_current_execution_resource_activation(admission)

    def _prepare(self, request: ModelRequest[Any]) -> dict[str, object]:
        context = self._request_preparation(request)
        sealed, frozen = self._frozen_preparation(context)
        callback_id = str(UUID(create_uuid_hex()))
        if sealed is None and frozen.activation_policy.has_model_call_boundary():
            # 同步接口只允许在无运行事件循环时等待；sealed 重入始终仅只读恢复。
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                frozen = asyncio.run(self._resource_activation.prepare_model_call(
                    parent=frozen, model_call_id=callback_id, checkpoint_ns=context.checkpoint_ns,
                ))
            else:
                raise RuntimeError("同步 model-call preparation 不能阻塞运行中的事件循环")
        return self._complete_preparation(request, context, sealed, frozen, callback_id)

    async def _aprepare(self, request: ModelRequest[Any]) -> dict[str, object]:
        context = self._request_preparation(request)
        sealed, frozen = self._frozen_preparation(context)
        callback_id = str(UUID(create_uuid_hex()))
        if sealed is None:
            frozen = await self._resource_activation.prepare_model_call(
                parent=frozen, model_call_id=callback_id, checkpoint_ns=context.checkpoint_ns,
            )
        return self._complete_preparation(request, context, sealed, frozen, callback_id)

    def _complete_preparation(
        self, request: ModelRequest[Any], context: _ProviderPreparation,
        sealed: ContextAssemblySnapshot | None, activation: ResourceActivationSnapshotRef,
        callback_id: str,
    ) -> dict[str, object]:
        admission = context.admission
        session_id, thread_id, turn_id = admission.session_id, admission.thread_id, admission.turn_id
        checkpoint_ns = context.checkpoint_ns
        include_structured_output = context.include_structured_output
        prepared = self._checkpointer.prepare_context_for_provider(
            session_id, thread_id,
            turn_id=turn_id, execution_id=admission.execution_id,
            plan_creation_idempotency_key=context.creation_key,
            seal_idempotency_key=context.seal_key,
            request_messages=request.messages,
            context_source_manager=self._context_source_manager,
            tool_set_supplier=lambda: self._tool_set_producer.snapshot(include_structured_output),
            provider_version=context.provider_version,
            wire_profile=context.wire_profile, wire_params=context.wire_params,
            target_format=context.target_format, checkpoint_ns=checkpoint_ns,
            model_call_id=callback_id if sealed is None else sealed.model_call_id,
            activation_snapshot=activation,
        )
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
            project_native = getattr(
                self._checkpointer, "project_context_plan_to_native", None
            )
            if not callable(project_native):
                raise TypeError(
                    "itemized context 缺少必需的 Saver native projector 端口"
                )
            native = project_native(
                session_id, thread_id, prepared["plan"], checkpoint_ns=checkpoint_ns
            )
            prepared["native_projection"] = native
            prepared["losses"] = tuple(native["losses"])
        logger.warning(
            "[itemized-context] provider prepare completed: session_id=%s turn_id=%s checkpoint_ns=%s assembly_id=%s",
            session_id,
            turn_id,
            checkpoint_ns,
            prepared.get("assembly_id"),
        )
        return prepared

    def _prepare_projection(
        self,
        request: ModelRequest[Any],
    ) -> tuple[dict[str, object], list[Any], list[Any]]:
        """执行 provider projection，校验 messages/tools 结构并记录结果与能力损失。"""
        return self._project_prepared(request, self._prepare(request))

    def _project_prepared(
        self, request: ModelRequest[Any], prepared: dict[str, object],
    ) -> tuple[dict[str, object], list[Any], list[Any]]:
        messages = prepared.get("messages")
        tools = prepared.get("tools")
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise TypeError("itemized provider projection 返回结构非法")
        if prepared["include_structured_output"]:
            # Saver 封存完整 ToolSet；框架会追加原注册的 structured tools，
            # handler 的普通 tools 因此只保留非结构化条目，避免发送两份 schema。
            structured_tool_names = frozenset(self._tool_set_producer.structured_tool_names)
            tools = [
                tool for tool in tools
                if tool_definition_name(tool) not in structured_tool_names
            ]
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

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        prepared, messages, tools = self._prepare_projection(request)
        # LangChain 的 provider handler 返回时，外层 astream_events 可能尚未
        # 消费 on_chat_model_start。prepared handle 必须保留到该事件由
        # RolloutCheckpointSaver 绑定真实 model_call_id 后再移除，不能在
        # middleware 的 handler 返回路径提前清理。
        with sealed_assembly_dispatch_scope(
            prepared["snapshot"], native_projection=prepared.get("native_projection"),
            callback_model_call_id=prepared["callback_model_call_id"],
        ):
            response = handler(
                request.override(
                    messages=messages,
                    system_message=None,
                    tools=tools,
                )
            )
            return _bind_response_model_call(
                response, model_call_id=prepared["callback_model_call_id"],
            )

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        delta_sink = get_current_model_delta_sink()
        reconcile = getattr(delta_sink, "complete_tool_from_message", None)
        if callable(reconcile):
            for message in request.messages:
                await reconcile(message)
        prepared, messages, tools = self._project_prepared(request, await self._aprepare(request))
        # 同步包装器的相同生命周期约束：真实 model-start event 消费前不
        # 得删除 Saver-owned prepared handle。
        with sealed_assembly_dispatch_scope(
            prepared["snapshot"], native_projection=prepared.get("native_projection"),
            callback_model_call_id=prepared["callback_model_call_id"],
        ):
            response = await handler(
                request.override(
                    messages=messages,
                    system_message=None,
                    tools=tools,
                )
            )
            return _bind_response_model_call(
                response, model_call_id=prepared["callback_model_call_id"],
            )


__all__ = ["SealedAssemblyDispatchBridge"]
