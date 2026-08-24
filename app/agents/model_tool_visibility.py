from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

from app.agents.tool_identity import tool_definition_name


class ModelToolVisibilityMiddleware(AgentMiddleware):
    """只从模型请求中隐藏工具，保留 Agent graph 的执行注册。"""

    def __init__(self, hidden_tool_names: Iterable[str]) -> None:
        self._hidden_tool_names = frozenset(hidden_tool_names)

    def _visible_request(self, request: ModelRequest) -> ModelRequest:
        if not self._hidden_tool_names:
            return request
        visible_tools = [
            tool
            for tool in request.tools
            if tool_definition_name(tool) not in self._hidden_tool_names
        ]
        return request.override(tools=visible_tools)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._visible_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._visible_request(request))
