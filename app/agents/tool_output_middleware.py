from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from app.agents.tool_identity import CUSTOM_TOOL_INVOKER_NAME
from app.services.infrastructure.tool_output_store import ToolOutputStore


class ToolOutputMiddleware(AgentMiddleware):
    """在工具结果进入模型上下文前统一物化过大的文本输出。"""

    def __init__(self, *, session_id: str, store: ToolOutputStore) -> None:
        if not session_id:
            raise ValueError("ToolOutputMiddleware 需要非空 session_id")
        self._session_id = session_id
        self._store = store

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[object]],
    ) -> ToolMessage | Command[object]:
        result = handler(request)
        return self._bound_result(request, result)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            Awaitable[ToolMessage | Command[object]],
        ],
    ) -> ToolMessage | Command[object]:
        result = await handler(request)
        if isinstance(result, Command) or not isinstance(result, ToolMessage):
            return result
        if result.status == "error":
            return result
        tool_name, tool_call_id = _tool_identity(request)
        return await self._store.abound(
            session_id=self._session_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            message=result,
        )

    def _bound_result(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Command[object],
    ) -> ToolMessage | Command[object]:
        if isinstance(result, Command) or not isinstance(result, ToolMessage):
            return result
        if result.status == "error":
            return result
        tool_name, tool_call_id = _tool_identity(request)
        return self._store.bound(
            session_id=self._session_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            message=result,
        )


def _tool_identity(request: ToolCallRequest) -> tuple[str, str]:
    tool_call = request.tool_call
    raw_name = tool_call.get("name")
    if not isinstance(raw_name, str) or not raw_name:
        raise RuntimeError("工具调用缺少 name，无法持久化输出")

    tool_name = raw_name
    arguments = tool_call.get("args")
    if raw_name == CUSTOM_TOOL_INVOKER_NAME and isinstance(arguments, Mapping):
        target_name = arguments.get("tool_name")
        if isinstance(target_name, str) and target_name.strip():
            tool_name = target_name.strip()

    tool_call_id = tool_call.get("id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise RuntimeError(f"{tool_name} 工具调用缺少 tool_call_id，无法持久化输出")
    return tool_name, tool_call_id
