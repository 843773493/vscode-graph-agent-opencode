from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool


CUSTOM_TOOL_INVOKER_NAME = "invoke_custom_tool"

ToolDefinitionLike = BaseTool | Callable[..., Any] | dict[str, Any]


def tool_definition_name(tool: ToolDefinitionLike) -> str:
    if isinstance(tool, BaseTool):
        return tool.name
    if isinstance(tool, dict):
        name = tool.get("name")
        if isinstance(name, str):
            return name
        function_def = tool.get("function")
        if isinstance(function_def, dict) and isinstance(function_def.get("name"), str):
            return function_def["name"]
        return ""
    return getattr(tool, "__name__", "")
