from __future__ import annotations

import asyncio
import contextvars
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from app.core.turn_execution_scope import (
    get_current_turn_execution_scope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)


@dataclass(frozen=True, slots=True)
class ThreadRuntimeBinding:
    """Agent 运行时绑定的精确 SessionThread 归属。

    归属只能由 Agent 后端在装配时提供，模型参数和工具 schema 都不携带它。
    调试等按 SessionThread 隔离的扩展工具通过该绑定把精确 owner 传给服务层，
    不再依赖服务层“裸 session 等价 main”的隐式归一。
    """

    session_id: str
    thread_id: str

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("ThreadRuntimeBinding.session_id 不能为空")
        if not self.thread_id.strip():
            raise ValueError("ThreadRuntimeBinding.thread_id 不能为空")


class ToolInvocationContext:
    """由 Agent 后端注入的单次工具调用上下文，不属于模型参数。"""

    def __init__(
        self,
        *,
        tool_timeout_seconds: float | None = None,
        thread_binding: ThreadRuntimeBinding | None = None,
    ) -> None:
        self._tool_call_id: contextvars.ContextVar[str | None] = (
            contextvars.ContextVar("agent_tool_call_id", default=None)
        )
        if tool_timeout_seconds is not None and tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds 必须大于 0")
        self.tool_timeout_seconds = tool_timeout_seconds
        #: 受信线程归属；为 None 时调用方（如直接构造 factory 的后端测试）
        #: 必须自行给出显式归属，不允许业务工具再退回裸 session_id。
        self.thread_binding = thread_binding

    def set_tool_call_id(
        self,
        tool_call_id: str,
    ) -> contextvars.Token[str | None]:
        if not tool_call_id:
            raise ValueError("tool_call_id 不能为空")
        return self._tool_call_id.set(tool_call_id)

    def reset_tool_call_id(
        self,
        token: contextvars.Token[str | None],
    ) -> None:
        self._tool_call_id.reset(token)

    def require_tool_call_id(self) -> str:
        tool_call_id = self._tool_call_id.get()
        if not tool_call_id:
            raise RuntimeError("当前工具执行上下文缺少 tool_call_id")
        return tool_call_id


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
        task = asyncio.create_task(handler(request))
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
    ) -> contextvars.Token[str | None]:
        tool_call_id = request.tool_call.get("id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            tool_name = request.tool_call.get("name")
            raise RuntimeError(
                "工具调用缺少 tool_call_id，无法建立后端调用上下文: "
                f"tool_name={tool_name!r}"
            )
        return self._context.set_tool_call_id(tool_call_id)


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
