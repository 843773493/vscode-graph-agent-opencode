"""已注册工具到管理目录定义的纯转换，不构建工具或执行图。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from app.agents.policy import ParsedCustomToolSpec, catalog_group_for_tool
from app.agents.policy.tool_groups import catalog_group_for_custom_tool

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from app.services.infrastructure.mcp.tool_definitions import ToolDefinition


def tools_to_catalog_definitions(
    registered_tools: Sequence[BaseTool],
    *,
    extension_definitions: Sequence[ToolDefinition] = (),
    custom_specs: Sequence[ParsedCustomToolSpec] = (),
) -> list[dict[str, Any]]:
    """将已注册工具转换为管理目录定义，参数投影与模型请求一致。"""
    from app.agents.model_tool_schema import export_model_tool_json_schema

    tool_map = {}
    for tool in registered_tools:
        if tool.name in tool_map:
            raise RuntimeError(f"工具命名冲突: {tool.name}")
        tool_map[tool.name] = tool
    if not tool_map:
        raise RuntimeError("Agent 工具注册为空，无法导出工具目录")
    specs_by_name = {spec.name: spec for spec in custom_specs}
    tools: list[dict[str, Any]] = []
    for tool_name, tool in tool_map.items():
        # tool_call_schema 是 LangChain 面向模型公开的权威参数模型；args_schema
        # 还包含 ToolRuntime 等运行时注入字段，不能用于工具目录或模型请求。
        parameters = export_model_tool_json_schema(tool)
        spec = specs_by_name.get(tool_name)
        group = catalog_group_for_custom_tool(spec) if spec is not None else catalog_group_for_tool(tool_name)
        group_fields = group.as_catalog_fields()
        tools.append(
            {
                "id": tool_name,
                "name": tool_name,
                "origin": "custom" if spec is not None else "builtin",
                "availability": "unknown" if spec is not None else None,
                "description": spec.description if spec is not None and spec.description else tool.description,
                "parameters": parameters,
                "category": group_fields["kind"],
                **group_fields,
            }
        )
    for definition in extension_definitions:
        metadata = definition.metadata
        if metadata.tool_id in tool_map:
            raise RuntimeError(f"扩展工具与 Agent 工具命名冲突: {metadata.tool_id}")
        tools.append({
            "id": metadata.tool_id, "name": metadata.tool_id, "origin": metadata.origin,
            "description": definition.description, "parameters": definition.input_schema,
            "category": metadata.kind, "kind": metadata.kind, "group_id": metadata.group_id,
            "group_name": f"扩展工具 · MCP · {metadata.group_id.removeprefix('mcp:')}",
        })
    return tools
