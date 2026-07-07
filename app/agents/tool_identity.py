from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool


def tool_definition_name(tool_def: BaseTool | dict[str, Any] | object) -> str | None:
    """解析 BaseTool、OpenAI function dict 等工具定义的名称。"""
    if isinstance(tool_def, BaseTool):
        return tool_def.name
    if isinstance(tool_def, dict):
        name = tool_def.get("name")
        if isinstance(name, str):
            return name
        function_def = tool_def.get("function")
        if isinstance(function_def, dict) and isinstance(function_def.get("name"), str):
            return str(function_def["name"])
        return None
    name = getattr(tool_def, "name", None)
    return name if isinstance(name, str) else None
