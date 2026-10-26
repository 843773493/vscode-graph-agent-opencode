from __future__ import annotations

from typing import Any, Literal

from langchain_core.messages import ToolCall, ToolMessage
from langgraph.prebuilt.tool_node import ToolNode
from langgraph.types import Command


class ContentPreservingToolNode(ToolNode):
    """沿用框架执行，只保留 ToolMessage 已校验的完整内容块。"""

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
