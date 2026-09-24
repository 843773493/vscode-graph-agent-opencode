"""把 Saver-owned itemized context 接到 LangChain model request 边界。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse, StateT
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.agents.sealed_assembly_dispatch import sealed_native_projection_scope
from app.core.job_context import get_current_job_id
from app.core.model_delta_context import get_current_model_delta_sink
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.request_plan import ContextContribution

logger = logging.getLogger(__name__)


def _runtime_session_id(request: ModelRequest[Any]) -> str:
    runtime = request.runtime
    if runtime is None:
        raise RuntimeError("itemized context middleware 缺少 runtime")
    execution_info = runtime.execution_info
    session_id = getattr(execution_info, "thread_id", None)
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("itemized context middleware 缺少 runtime thread_id")
    return session_id


def _runtime_checkpoint_ns(request: ModelRequest[Any]) -> str:
    runtime = request.runtime
    if runtime is None:
        raise RuntimeError("itemized context middleware 缺少 runtime")
    execution_info = runtime.execution_info
    checkpoint_ns = getattr(execution_info, "checkpoint_ns", "")
    if not isinstance(checkpoint_ns, str):
        raise TypeError("itemized context middleware checkpoint_ns 非法")
    return checkpoint_ns


def _request_turn_id(request: ModelRequest[Any]) -> str:
    turn_id = get_current_job_id()
    if turn_id:
        return turn_id
    runtime = request.runtime
    execution_info = runtime.execution_info if runtime is not None else None
    fallback = getattr(execution_info, "thread_id", None)
    if isinstance(fallback, str) and fallback:
        return fallback
    raise RuntimeError("itemized context middleware 缺少当前 Turn identity")


def _request_idempotency_keys(
    request: ModelRequest[Any],
    *,
    session_id: str,
    turn_id: str,
    execution_id: str,
    checkpoint_ns: str,
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
        }
    )
    return f"provider-create:{identity}", f"provider-seal:{identity}"


def _system_blocks(message: SystemMessage | None) -> list[dict[str, object]]:
    if message is None:
        return []
    result: list[dict[str, object]] = []
    for index, block in enumerate(message.content_blocks):
        if not isinstance(block, Mapping):
            raise TypeError(
                "ModelRequest.system_message.content_blocks 中出现非法 block: "
                f"index={index}"
            )
        result.append({str(key): value for key, value in block.items()})
    return result


def _tool_snapshot(
    tools: Sequence[BaseTool | Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    for index, tool in enumerate(tools):
        if isinstance(tool, BaseTool):
            result.append(convert_to_openai_tool(tool))
            continue
        if isinstance(tool, Mapping):
            result.append({str(key): value for key, value in tool.items()})
            continue
        raise TypeError(f"ModelRequest.tools[{index}] 不是合法工具定义")
    return tuple(result)


def _target_format(model: object) -> str:
    name = type(model).__name__.lower()
    return "responses" if "responses" in name else "chat_completions"


def _prompt_contributions(
    message: SystemMessage | None,
) -> tuple[ContextContribution, ...]:
    blocks = _system_blocks(message)
    if not blocks:
        return ()
    slot_hash = sha256_jcs({"source": "sealed_request", "slot": "system"})
    return (
        ContextContribution(
            contribution_id=f"prompt-slot:{slot_hash}",
            source_kind="sealed_request:system",
            source_revision=sha256_jcs(blocks),
            content_hash=contribution_content_hash("prompt", blocks),
            request_only=True,
            metadata={
                "label": "assembled system prompt",
                "operation": "replace",
                "runtime_projection": True,
            },
            contribution_kind="prompt",
            body=blocks,
            content_length=len(canonical_json_bytes(blocks)),
            # 这是唯一可原位更新 revision 的 replaceable source slot；
            # 替换/安全判定由 typed core 字段承载，metadata 中的同名 key
            # 已物理下线，不再拥有任何解释权。
            replaceable_source=True,
            # assembled system prompt 是唯一 root producer slot；显式声明
            # root_eligible，projector 按它编译唯一 system root。
            root_placement="root_eligible",
        ),
    )


class SealedAssemblyDispatchBridge(AgentMiddleware[StateT, Any, Any]):
    """无状态地把 Saver sealed selection 转发给 Provider handler。"""

    def __init__(self, *, checkpointer: object) -> None:
        self._checkpointer = checkpointer

    def _prepare(self, request: ModelRequest[Any]) -> dict[str, object]:
        session_id = _runtime_session_id(request)
        checkpoint_ns = _runtime_checkpoint_ns(request)
        turn_id = _request_turn_id(request)
        prepare = getattr(self._checkpointer, "prepare_context_for_provider", None)
        supports = getattr(self._checkpointer, "supports_itemized_context", None)
        if not callable(prepare) or not callable(supports):
            raise TypeError("itemized context 缺少必需的 Saver prepare/supports 端口")
        if not supports(session_id, checkpoint_ns=checkpoint_ns):
            raise RuntimeError("itemized context 必须使用可用的 v2 Saver owner")
        execution_for_turn = getattr(self._checkpointer, "execution_for_turn", None)
        if not callable(execution_for_turn):
            raise TypeError("itemized context 缺少必需的 Saver execution_for_turn 端口")
        creation_key, seal_key = _request_idempotency_keys(
            request,
            session_id=session_id,
            turn_id=turn_id,
            checkpoint_ns=checkpoint_ns,
            execution_id=execution_for_turn(
                session_id, turn_id=turn_id, checkpoint_ns=checkpoint_ns
            ),
        )
        model_name = getattr(request.model, "model_name", None) or getattr(
            request.model, "model", None
        )
        provider_version = str(model_name or type(request.model).__name__)
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
        prepared = prepare(
            session_id,
            turn_id=turn_id,
            plan_creation_idempotency_key=creation_key,
            seal_idempotency_key=seal_key,
            request_messages=request.messages,
            prompt_contributions=_prompt_contributions(request.system_message),
            tool_snapshot=_tool_snapshot(request.tools or ()),
            provider_version=provider_version,
            target_format=_target_format(request.model),
            checkpoint_ns=checkpoint_ns,
        )
        if _target_format(request.model) == "responses":
            project_native = getattr(
                self._checkpointer, "project_context_plan_to_native", None
            )
            if not callable(project_native):
                raise TypeError(
                    "itemized context 缺少必需的 Saver native projector 端口"
                )
            native = project_native(
                session_id, prepared["plan"], checkpoint_ns=checkpoint_ns
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

    def _cleanup(
        self, request: ModelRequest[Any], prepared: Mapping[str, object]
    ) -> None:
        assembly_id = prepared.get("assembly_id")
        if not isinstance(assembly_id, str) or not assembly_id:
            return
        discard = getattr(
            self._checkpointer,
            "discard_prepared_context_for_dispatch",
            None,
        )
        if callable(discard):
            discard(
                _runtime_session_id(request),
                turn_id=_request_turn_id(request),
                assembly_id=assembly_id,
                checkpoint_ns=_runtime_checkpoint_ns(request),
            )

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        prepared = self._prepare(request)
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
                _runtime_session_id(request),
                _request_turn_id(request),
                losses,
            )
        # LangChain 的 provider handler 返回时，外层 astream_events 可能尚未
        # 消费 on_chat_model_start。prepared handle 必须保留到该事件由
        # RolloutCheckpointSaver 绑定真实 model_call_id 后再移除，不能在
        # middleware 的 handler 返回路径提前清理。
        with sealed_native_projection_scope(prepared.get("native_projection")):
            return handler(
                request.override(
                    messages=messages,
                    system_message=None,
                    tools=tools,
                )
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
        prepared = self._prepare(request)
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
                _runtime_session_id(request),
                _request_turn_id(request),
                losses,
            )
        # 同步包装器的相同生命周期约束：真实 model-start event 消费前不
        # 得删除 Saver-owned prepared handle。
        with sealed_native_projection_scope(prepared.get("native_projection")):
            return await handler(
                request.override(
                    messages=messages,
                    system_message=None,
                    tools=tools,
                )
            )


__all__ = ["SealedAssemblyDispatchBridge"]
