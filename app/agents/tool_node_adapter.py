"""适配 LangGraph ToolNode 的审批执行、原调用身份和完整结果内容。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolNode, ToolRuntime
from langgraph.types import Command

from app.agents.tool_confirmation_middleware import (
    approved_tool_call,
    complete_tool_approval_results,
)
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.core.execution_pause import execution_pause_boundary
from app.core.shielded_cleanup import run_sync_shielded


class DerivedToolPolicyMiddleware(AgentMiddleware):
    """派生只收窄实际调用权限，模型可见 schema 仍取冻结的来源。"""

    def __init__(self, allowed_tool_ids: tuple[str, ...], permit: Callable[[str], bool] | None = None) -> None:
        self._allowed = frozenset(allowed_tool_ids)
        self._permit = permit

    def _denial(self, request: ToolCallRequest) -> ToolMessage | None:
        call = approved_tool_call(request.state, request.tool_call)
        target = call["name"]
        if target == EXTENSION_TOOL_INVOKER_NAME:
            arguments = call.get("args")
            target = arguments.get("tool_name") if isinstance(arguments, Mapping) else None
        if (not isinstance(target, str) or target not in self._allowed
                or self._permit is not None and not self._permit(target)):
            return ToolMessage(
                content=f"derived-tool-denied: 本次派生未授权或当前策略已撤销工具 {target!r}",
                name=request.tool_call["name"], tool_call_id=request.tool_call["id"], status="error",
            )
        return None

    @staticmethod
    def _result(result: ToolMessage | Command) -> ToolMessage:
        if not isinstance(result, ToolMessage):
            raise TypeError("derived-tool-command-unsupported: 派生工具不得修改 Graph 状态；已发生的操作仍由原 owner 保存")
        return result

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage:
        denied = self._denial(request)
        return denied if denied is not None else self._result(handler(request))

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage:
        denied = await run_sync_shielded(self._denial, request)
        return denied if denied is not None else self._result(await handler(request))


class AgentToolNode(ToolNode):
    """沿用框架调度，应用审批覆盖、保留原调用配对和完整内容块。"""

    async def _arun_one(
        self, call: ToolCall, input_type: Literal["list", "dict", "tool_calls"],
        tool_runtime: ToolRuntime,
    ) -> ToolMessage | Command | list[Command | ToolMessage]:
        # 暂停等待发生在所有工具中间件之前，尚未准入时不创建局部超时 scope。
        async with execution_pause_boundary():
            return await super()._arun_one(call, input_type, tool_runtime)

    def _apply_tool_approval(self, request: ToolCallRequest) -> ToolCallRequest:
        approved_call = approved_tool_call(request.state, request.tool_call)
        if approved_call is request.tool_call:
            return request
        return replace(request, tool_call=approved_call, tool=self.tools_by_name.get(approved_call["name"]))

    # TODO: 上游支持在执行端解析审批覆盖后，移除这两个私有入口适配。
    # 在现有身份／结果中间件之后覆盖，保证 emitting call 始终取自原 AI carrier。
    def _execute_tool_sync(self, request: ToolCallRequest, input_type, config: RunnableConfig):
        execution_request = self._apply_tool_approval(request)
        result = super()._execute_tool_sync(execution_request, input_type, config)
        return self._restore_emitting_tool_name(result, request.tool_call, input_type) if execution_request is not request else result

    async def _execute_tool_async(self, request: ToolCallRequest, input_type, config: RunnableConfig):
        execution_request = self._apply_tool_approval(request)
        result = await super()._execute_tool_async(execution_request, input_type, config)
        if execution_request is not request:
            result = self._restore_emitting_tool_name(result, request.tool_call, input_type)
            if execution_request.tool is None:
                assert isinstance(result, ToolMessage)
                await complete_tool_approval_results(request.state, (result,))
        return result

    def _restore_emitting_tool_name(
        self, result: ToolMessage | Command | list[Command | ToolMessage], emitting_call: ToolCall,
        input_type: Literal["list", "dict", "tool_calls"],
    ) -> ToolMessage | Command | list[Command | ToolMessage]:
        # 结果属于原模型调用；实际执行目标仍由审批决定、工具事件和输出引用记录。
        if isinstance(result, ToolMessage):
            return result.model_copy(update={"name": emitting_call["name"]})
        if isinstance(result, Command):
            return self._validate_tool_command(result, emitting_call, input_type)
        normalized = [item.model_copy(update={"name": emitting_call["name"]}) if isinstance(item, ToolMessage) else item
                      for item in result]
        return self._validate_tool_command_list(normalized, emitting_call, input_type)

    # TODO: 上游 ToolNode 的内容白名单支持标准音视频块后，移除此私有协议适配。
    def _normalize_tool_response(
        self, response: Any, tool_call: ToolCall,
        input_type: Literal["list", "dict", "tool_calls"],
    ) -> ToolMessage | Command | list[Command | ToolMessage]:
        if isinstance(response, ToolMessage):
            return response
        return super()._normalize_tool_response(response, tool_call, input_type)

    def _validate_tool_command_list(
        self, response: list[Command | ToolMessage], tool_call: ToolCall,
        input_type: Literal["list", "dict", "tool_calls"],
    ) -> list[Command | ToolMessage]:
        messages = [item for item in response if isinstance(item, ToolMessage)]
        for item in response:
            if isinstance(item, Command) and isinstance(item.update, dict):
                messages.extend(message for message in item.update.get(self._messages_key, [])
                                if isinstance(message, ToolMessage))
        if sum(message.tool_call_id == tool_call["id"] for message in messages) != 1:
            raise ValueError(f"工具 {tool_call['name']} 必须恰好返回一个与 {tool_call['id']!r} 配对的终态结果")
        return [self._validate_tool_command(item, tool_call, input_type, require_terminator=False)
                if isinstance(item, Command) else item for item in response]


def adapt_agent_tool_node(node: ToolNode) -> ToolNode:
    """用项目 ToolNode 重建同一节点，保留原工具、注入、错误策略和中间件。"""
    return AgentToolNode(
        list(node.tools_by_name.values()), name=node.name,
        tags=list(node.tags) if node.tags is not None else None,
        handle_tool_errors=node._handle_tool_errors, messages_key=node._messages_key,
        wrap_tool_call=node._wrap_tool_call, awrap_tool_call=node._awrap_tool_call,
    )


def extract_agent_tools_by_name(agent: Any) -> dict[str, Any]:
    """提取图中 ToolNode 的已注册工具，集中隔离框架节点结构访问。"""
    graph_view = agent.get_graph()
    nodes = getattr(graph_view, "nodes", {}) or {}
    tool_map: dict[str, Any] = {}
    for node in nodes.values():
        candidate = getattr(node, "data", node)
        tools_by_name = getattr(candidate, "tools_by_name", None)
        if isinstance(tools_by_name, dict):
            tool_map.update(tools_by_name)
    return tool_map
