from __future__ import annotations

import asyncio
import contextvars
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from app.core.identifier import uuid7_datetime_from_hex, validate_thread_id
from app.core.model_delta_context import (
    get_current_model_delta_sink,
    model_tool_invocation_scope,
)
from app.core.shielded_cleanup import run_cleanup_shielded
from app.core.turn_execution_scope import (
    ScopeCancelledError,
    get_current_turn_execution_scope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import (
    matches_tool_call_id,
    provider_tool_call_id,
    software_tool_operation_id,
)
from app.schemas.internal_v2.text_mutation import (
    TextMutationReceiptDTO,
)
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingRef,
)
from app.services.infrastructure.mcp.invocation import (
    mcp_cancelled_receipt,
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
    """ToolNode 的 Provider 原始调用 ID；ModelCall 归属独立保存在 projection_identity。"""

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
        return self._invocation.set(
            _ToolInvocationBinding(tool_call_id, projection_identity)
        )

    def reset_tool_call_id(
        self,
        token: contextvars.Token[_ToolInvocationBinding | None],
    ) -> None:
        self._invocation.reset(token)

    def require_tool_call_id(self) -> str:
        """返回 Provider 原始调用 ID，不拼接 canonical carrier 的 ModelCall scope。"""
        invocation = self._invocation.get()
        if invocation is None:
            raise RuntimeError("当前工具执行上下文缺少 tool_call_id")
        return invocation.tool_call_id

    def require_model_call_id(self) -> str:
        """返回 emitting ModelCall 的稳定身份，不从工具调用 ID 反推。"""
        invocation = self._invocation.get()
        identity = invocation.projection_identity if invocation is not None else None
        if identity is None or identity.model_call_id is None:
            raise RuntimeError(
                "当前工具执行上下文缺少 emitting assistant 的 model_call_id"
            )
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
        call_id = provider_tool_call_id(
            invocation.projection_identity, invocation.tool_call_id
        )
        return software_tool_operation_id(
            operation,
            self.thread_binding.session_id,
            self.thread_binding.thread_id,
            model_call_id,
            call_id,
        )

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
                await delta_sink.prepare_tool_execution(
                    self._context.require_model_call_id()
                )
            try:
                if delta_sink is None:
                    return await handler(request)
                with model_tool_invocation_scope(
                    self._context.require_model_call_id(),
                    self._context.require_tool_call_id(),
                ):
                    return await handler(request)
            except asyncio.CancelledError as error:
                mcp_receipt = mcp_cancelled_receipt(error)
                if delta_sink is not None and mcp_receipt is not None:
                    async def commit_mcp_cancellation() -> None:
                        await delta_sink.fail_tool_invocation(
                            self._context.require_model_call_id(), self._context.require_tool_call_id(),
                            error=mcp_receipt.model_dump_json(), artifact=mcp_receipt.as_artifact(),
                        )

                    await run_cleanup_shielded(commit_mcp_cancellation)
                if (
                    delta_sink is not None
                    and len(error.args) == 1
                    and isinstance(error.args[0], TextMutationReceiptDTO)
                ):
                    receipt = error.args[0]

                    async def commit_file_cancellation() -> None:
                        await delta_sink.fail_tool_invocation(
                            self._context.require_model_call_id(),
                            self._context.require_tool_call_id(),
                            error=receipt.model_dump_json(exclude_none=True),
                        )

                    await run_cleanup_shielded(commit_file_cancellation)
                raise
            except (TimeoutError, ScopeCancelledError):
                raise
            except Exception as error:
                # 原工具异常只能在同一调用的 canonical 失败结果提交后交给
                # LangGraph，避免 task error 先落盘而 ToolResult 仍未提交。
                if delta_sink is not None:
                    await delta_sink.fail_tool_invocation(
                        self._context.require_model_call_id(),
                        self._context.require_tool_call_id(),
                        error=str(error),
                    )
                raise

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
        execution_state = request.state
        execution_messages = (
            execution_state.get("messages", ())
            if isinstance(execution_state, Mapping)
            else ()
        )
        assistant_carrier = next(
            (
                message
                for message in reversed(execution_messages)
                if isinstance(message, AIMessage)
            ),
            None,
        )
        projection_identity = None
        if assistant_carrier is not None:
            model_call_id = LangChainMessageCodec().model_call_id(assistant_carrier)
            if model_call_id is not None:
                candidate_projection_identity = ItemProjectionIdentity(
                    projection_message_id=assistant_carrier.id,
                    model_call_id=model_call_id,
                )
                tool_call_name = request.tool_call.get("name")
                if any(
                    assistant_tool_call.get("name") == tool_call_name
                    and isinstance(assistant_tool_call.get("id"), str)
                    and matches_tool_call_id(
                        candidate_projection_identity,
                        assistant_tool_call["id"],
                        tool_call_id,
                    )
                    for assistant_tool_call in assistant_carrier.tool_calls
                ):
                    projection_identity = candidate_projection_identity
        return self._context.set_tool_call_id(
            tool_call_id,
            projection_identity=projection_identity,
        )


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
    cause = error.__cause__
    mcp_receipt = mcp_cancelled_receipt(cause)
    if mcp_receipt is not None:
        return ToolMessage(
            content=mcp_receipt.model_dump_json(), artifact=mcp_receipt.as_artifact(),
            tool_call_id=tool_call_id, status="error",
        )
    if (
        isinstance(cause, asyncio.CancelledError)
        and len(cause.args) == 1
        and isinstance(cause.args[0], TextMutationReceiptDTO)
    ):
        # 复用取消时已提交的原回执，不能把未知写入投影成可重试的通用超时。
        return ToolMessage(
            content=cause.args[0].model_dump_json(exclude_none=True),
            tool_call_id=tool_call_id,
            status="error",
        )
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
