from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from app.core.identifier import uuid7_datetime_from_hex
from app.core.model_delta_context import get_current_model_delta_sink
from app.core.session_catalog_store import validate_thread_id
from app.core.turn_execution_scope import (
    get_current_turn_execution_scope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import (
    matches_tool_call_id,
    provider_tool_call_id,
)
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingRef,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)


@dataclass(frozen=True, slots=True)
class ThreadRuntimeBinding:
    """Agent 运行时绑定的精确 SessionThread 归属。

    归属只能由 Agent 后端在装配时提供，模型参数和工具 schema 都不携带它。
    调试等按 SessionThread 隔离的扩展工具通过该绑定把 canonical owner 传给服务层，
    不再接受逻辑 main 或 Session ID 作为 thread_id。
    """

    session_id: str
    thread_id: str

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("ThreadRuntimeBinding.session_id 不能为空")
        validate_thread_id(self.thread_id)


@dataclass(frozen=True, slots=True)
class _ToolInvocationBinding:
    tool_call_id: str
    projection_identity: ItemProjectionIdentity | None


class ToolInvocationContext:
    """由 Agent 后端注入的单次工具调用上下文，不属于模型参数。"""

    def __init__(
        self,
        *,
        tool_timeout_seconds: float | None = None,
        thread_binding: ThreadRuntimeBinding | None = None,
    ) -> None:
        self._invocation: contextvars.ContextVar[_ToolInvocationBinding | None] = (
            contextvars.ContextVar("agent_tool_invocation", default=None)
        )
        self._extension_catalog_binding: contextvars.ContextVar[
            ExtensionCatalogBindingRef | None
        ] = contextvars.ContextVar("extension_catalog_binding", default=None)
        if tool_timeout_seconds is not None and tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds 必须大于 0")
        self.tool_timeout_seconds = tool_timeout_seconds
        #: 受信线程归属；为 None 时调用方（如直接构造 factory 的后端测试）
        #: 必须自行给出显式归属，不允许业务工具再退回裸 session_id。
        self.thread_binding = thread_binding

    def set_tool_call_id(
        self,
        tool_call_id: str,
        *,
        projection_identity: ItemProjectionIdentity | None = None,
    ) -> contextvars.Token[_ToolInvocationBinding | None]:
        if not tool_call_id:
            raise ValueError("tool_call_id 不能为空")
        return self._invocation.set(_ToolInvocationBinding(tool_call_id, projection_identity))

    def reset_tool_call_id(
        self,
        token: contextvars.Token[_ToolInvocationBinding | None],
    ) -> None:
        self._invocation.reset(token)

    def require_tool_call_id(self) -> str:
        invocation = self._invocation.get()
        if invocation is None:
            raise RuntimeError("当前工具执行上下文缺少 tool_call_id")
        return invocation.tool_call_id

    def require_model_call_id(self) -> str:
        invocation = self._invocation.get()
        identity = invocation.projection_identity if invocation is not None else None
        if identity is None or identity.model_call_id is None:
            raise RuntimeError("当前工具执行上下文缺少 emitting assistant 的 model_call_id")
        return identity.model_call_id

    def require_operation_created_at(self) -> datetime:
        """沿原 emitting model-call 的 UUIDv7 时刻恢复，重试不延长窗口。"""
        identity = UUID(self.require_model_call_id())
        if identity.version != 7:
            raise ValueError("通信 operation 必须来自受信 UUIDv7 model call")
        return uuid7_datetime_from_hex(identity.hex)

    def require_operation_id(self, operation: str) -> str:
        """软件操作身份来自已持久化的 emitting model call，不进入模型 schema。"""
        invocation = self._invocation.get()
        model_call_id = self.require_model_call_id()
        if invocation is None or self.thread_binding is None:
            raise RuntimeError("通信工具缺少精确 thread invocation binding")
        call_id = provider_tool_call_id(invocation.projection_identity, invocation.tool_call_id)
        preimage = json.dumps([operation, self.thread_binding.session_id,
                              self.thread_binding.thread_id, model_call_id, call_id],
                             ensure_ascii=False, separators=(",", ":"))
        return "cop_" + hashlib.sha256(preimage.encode()).hexdigest()

    def set_extension_catalog_binding(
        self,
        binding: ExtensionCatalogBindingRef,
    ) -> contextvars.Token[ExtensionCatalogBindingRef | None]:
        return self._extension_catalog_binding.set(binding)

    def reset_extension_catalog_binding(
        self,
        token: contextvars.Token[ExtensionCatalogBindingRef | None],
    ) -> None:
        self._extension_catalog_binding.reset(token)

    def current_extension_catalog_binding(
        self,
    ) -> ExtensionCatalogBindingRef | None:
        return self._extension_catalog_binding.get()


class ToolInvocationContextMiddleware(AgentMiddleware):
    """在统一执行层注入调用身份，业务工具通过闭包读取。"""

    def __init__(self, context: ToolInvocationContext) -> None:
        self._context = context

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[object]],
    ) -> ToolMessage | Command[object]:
        token = self._bind(request)
        try:
            return handler(request)
        finally:
            self._context.reset_tool_call_id(token)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            Awaitable[ToolMessage | Command[object]],
        ],
    ) -> ToolMessage | Command[object]:
        token = self._bind(request)
        parent_scope = get_current_turn_execution_scope()
        tool_scope = (
            parent_scope.child(
                f"tool-{request.tool_call.get('id')}",
                timeout_seconds=self._context.tool_timeout_seconds,
            )
            if parent_scope is not None
            else None
        )
        scope_token = (
            set_current_turn_execution_scope(tool_scope)
            if tool_scope is not None
            else None
        )
        async def invoke_tool() -> ToolMessage | Command[object]:
            delta_sink = get_current_model_delta_sink()
            if delta_sink is not None:
                await delta_sink.prepare_tool_execution(self._context.require_model_call_id())
            return await handler(request)

        task = asyncio.create_task(invoke_tool())
        abort_hook_id = (
            tool_scope.register_abort(lambda _reason: _cancel_task(task))
            if tool_scope is not None
            else None
        )
        try:
            if self._context.tool_timeout_seconds is None:
                return await task
            try:
                return await asyncio.wait_for(task, self._context.tool_timeout_seconds)
            except TimeoutError as error:
                if tool_scope is not None:
                    await tool_scope.cancel("scope_deadline_exceeded")
                return _timeout_tool_message(
                    request,
                    error,
                    timeout_ms=int(self._context.tool_timeout_seconds * 1000),
                )
        except TimeoutError as error:
            # 终端/浏览器客户端的传输超时可能发生在工具自己的局部等待之外。
            # 这类异常没有对应的 on_tool_end 时，LangGraph 会直接结束 AgentLoop，
            # 前端只能看到笼统的 execution_error。转换成带 tool_call_id 的失败
            # ToolMessage，模型可以检查状态后重试，且不会把整轮误报为用户中断。
            return _timeout_tool_message(request, error)
        except asyncio.CancelledError:
            if tool_scope is not None:
                tool_scope.raise_if_cancelled()
            raise
        finally:
            if tool_scope is not None and abort_hook_id is not None:
                tool_scope.remove_abort(abort_hook_id)
            if scope_token is not None:
                reset_current_turn_execution_scope(scope_token)
            if tool_scope is not None:
                await tool_scope.close()
            self._context.reset_tool_call_id(token)

    def _bind(
        self,
        request: ToolCallRequest,
    ) -> contextvars.Token[_ToolInvocationBinding | None]:
        tool_call_id = request.tool_call.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            tool_name = request.tool_call.get("name")
            raise RuntimeError(
                "工具调用缺少 tool_call_id，无法建立后端调用上下文: "
                f"tool_name={tool_name!r}"
            )
        # ToolNode 的本次 request 来自 state 最后一个 AI carrier；只核该
        # carrier 的实际调用，不搜索历史消息或最新的 durable model-call。
        state = request.state
        messages = state.get("messages", ()) if isinstance(state, Mapping) else ()
        assistant = next(
            (message for message in reversed(messages) if isinstance(message, AIMessage)),
            None,
        )
        identity = None
        if assistant is not None:
            model_call_id = LangChainMessageCodec().model_call_id(assistant)
            if model_call_id is not None:
                candidate = ItemProjectionIdentity(
                    projection_message_id=assistant.id, model_call_id=model_call_id,
                )
                if any(
                    call.get("name") == request.tool_call.get("name")
                    and isinstance(call.get("id"), str)
                    and matches_tool_call_id(candidate, call["id"], tool_call_id)
                    for call in assistant.tool_calls
                ):
                    identity = candidate
        return self._context.set_tool_call_id(tool_call_id, projection_identity=identity)


async def _cancel_task(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _timeout_tool_message(
    request: ToolCallRequest,
    error: TimeoutError,
    *,
    timeout_ms: int | None = None,
) -> ToolMessage:
    """把工具下游超时投影成可配对、可恢复的工具结果。"""
    tool_call_id = request.tool_call.get("id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise RuntimeError("工具超时缺少 tool_call_id") from error
    tool_name = request.tool_call.get("name") or "unknown_tool"
    timeout_label = "工具执行超过局部超时" if timeout_ms is not None else "工具执行超时"
    payload: dict[str, object] = {
        "status": "error",
        "code": "tool_execution_timeout",
        "error": (
            f"{timeout_label}: tool={tool_name}, call_id={tool_call_id}；"
            f"下游操作结果未确认: {error}"
        ),
        "retryable": True,
        "recovery": "check_tool_state_before_retry",
    }
    if timeout_ms is not None and timeout_ms > 0:
        payload["timeoutMs"] = timeout_ms
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        tool_call_id=tool_call_id,
        status="error",
    )


__all__ = [
    "ThreadRuntimeBinding",
    "ToolInvocationContext",
    "ToolInvocationContextMiddleware",
]
