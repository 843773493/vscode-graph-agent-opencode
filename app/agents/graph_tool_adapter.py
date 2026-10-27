from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolNode
from langgraph.types import Command

from app.agents.tool_confirmation_middleware import (
    approved_tool_call,
    complete_tool_approval_results,
)


class ContentPreservingToolNode(ToolNode):
    """沿用框架执行，只保留 ToolMessage 已校验的完整内容块。"""

    def _approval_request(self, request: ToolCallRequest) -> ToolCallRequest:
        edited = approved_tool_call(request.state, request.tool_call)
        if edited is request.tool_call:
            return request
        return replace(request, tool_call=edited, tool=self.tools_by_name.get(edited["name"]))

    # TODO: 上游支持在执行端解析审批覆盖后，移除这两个私有入口适配。
    # 在现有身份／结果中间件之后覆盖，保证 emitting call 始终取自原 AI carrier。
    def _execute_tool_sync(self, request: ToolCallRequest, input_type, config: RunnableConfig):
        approved = self._approval_request(request)
        result = super()._execute_tool_sync(approved, input_type, config)
        return self._approval_response(result, request.tool_call, input_type) if approved is not request else result

    async def _execute_tool_async(self, request: ToolCallRequest, input_type, config: RunnableConfig):
        approved = self._approval_request(request)
        result = await super()._execute_tool_async(approved, input_type, config)
        if approved is not request:
            result = self._approval_response(result, request.tool_call, input_type)
            if approved.tool is None:
                assert isinstance(result, ToolMessage)
                await complete_tool_approval_results(request.state, (result,))
        return result

    def _approval_response(
        self, result: ToolMessage | Command | list[Command | ToolMessage], original: ToolCall,
        input_type: Literal["list", "dict", "tool_calls"],
    ) -> ToolMessage | Command | list[Command | ToolMessage]:
        # 结果属于原模型调用；实际执行目标仍由审批决定、工具事件和输出引用记录。
        if isinstance(result, ToolMessage):
            return result.model_copy(update={"name": original["name"]})
        if isinstance(result, Command):
            return self._validate_tool_command(result, original, input_type)
        normalized = [item.model_copy(update={"name": original["name"]}) if isinstance(item, ToolMessage) else item
                      for item in result]
        return self._validate_tool_command_list(normalized, original, input_type)

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


def content_preserving_tool_node(node: ToolNode) -> ToolNode:
    """重建同一执行节点，保留原工具、注入、错误策略和中间件。"""
    return ContentPreservingToolNode(
        list(node.tools_by_name.values()), name=node.name,
        tags=list(node.tags) if node.tags is not None else None,
        handle_tool_errors=node._handle_tool_errors, messages_key=node._messages_key,
        wrap_tool_call=node._wrap_tool_call, awrap_tool_call=node._awrap_tool_call,
    )


def extract_agent_tools_by_name(agent: Any) -> dict[str, Any]:
    """从 DeepAgents 图中提取工具映射，集中隔离第三方私有结构访问。"""
    graph_view = agent.get_graph()
    nodes = getattr(graph_view, "nodes", {}) or {}
    tool_map: dict[str, Any] = {}
    for node in nodes.values():
        candidate = getattr(node, "data", node)
        tools_by_name = getattr(candidate, "tools_by_name", None)
        if isinstance(tools_by_name, dict):
            tool_map.update(tools_by_name)
    return tool_map
