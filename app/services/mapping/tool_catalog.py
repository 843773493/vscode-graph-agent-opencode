"""已注册工具到管理目录定义的纯转换，不构建工具或执行图。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from app.agents.policy import catalog_group_for_tool

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool


def tools_to_catalog_definitions(
    registered_tools: Sequence[BaseTool],
    *,
    extension_tools: Sequence[BaseTool] = (),
) -> list[dict[str, Any]]:
    """将已注册工具转换为管理目录定义，参数投影与模型请求一致。"""
    from app.agents.model_tool_schema import export_model_tool_json_schema

    tool_map = {tool.name: tool for tool in registered_tools}
    if not tool_map:
        raise RuntimeError("Agent 工具注册为空，无法导出工具目录")
    for tool in extension_tools:
        if tool.name in tool_map:
            raise RuntimeError(f"扩展工具与 Agent 工具命名冲突: {tool.name}")
        tool_map[tool.name] = tool

    tools: list[dict[str, Any]] = []
    for tool_name, tool in tool_map.items():
        # tool_call_schema 是 LangChain 面向模型公开的权威参数模型；args_schema
        # 还包含 ToolRuntime 等运行时注入字段，不能用于工具目录或模型请求。
        parameters = export_model_tool_json_schema(tool)
        metadata = dict(getattr(tool, "metadata", None) or {})
        mcp_server_id = metadata.get("mcp_server_id")
        group = catalog_group_for_tool(tool_name)
        group_fields = (
            {
                "group_id": f"mcp:{mcp_server_id}",
                "group_name": f"扩展工具 · MCP · {mcp_server_id}",
                "kind": "extension",
            }
            if isinstance(mcp_server_id, str) and mcp_server_id
            else group.as_catalog_fields()
        )
        tools.append(
            {
                "id": tool_name,
                "name": tool_name,
                "origin": (
                    "mcp"
                    if isinstance(mcp_server_id, str) and mcp_server_id
                    else "builtin"
                ),
                "description": getattr(tool, "description", ""),
                "parameters": parameters,
                "category": group_fields["kind"],
                **group_fields,
            }
        )
    return tools
