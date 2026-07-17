from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command


class ToolInvocationContext:
    """由 Agent 后端注入的单次工具调用上下文，不属于模型参数。"""

    def __init__(self) -> None:
        self._tool_call_id: contextvars.ContextVar[str | None] = (
            contextvars.ContextVar("agent_tool_call_id", default=None)
        )

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
        try:
            return await handler(request)
        finally:
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


__all__ = [
    "ToolInvocationContext",
    "ToolInvocationContextMiddleware",
]
