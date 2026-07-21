from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel


def get_model_tool_schema(tool: BaseTool) -> type[BaseModel]:
    """返回 LangChain 面向模型公开的权威 Pydantic 工具参数模型。"""
    tool_call_schema = getattr(tool, "tool_call_schema", None)
    if not isinstance(tool_call_schema, type) or not issubclass(
        tool_call_schema,
        BaseModel,
    ):
        raise TypeError(
            "工具缺少模型可见的 Pydantic tool_call_schema: "
            f"tool_name={tool.name} tool_type={type(tool).__name__}"
        )
    return tool_call_schema


def validate_model_tool_arguments(
    tool: BaseTool,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """按模型可见 schema 校验参数，不接受或读取内部 args_schema。"""
    if not isinstance(arguments, dict):
        raise TypeError(
            "模型工具参数必须是 object: "
            f"tool_name={tool.name} actual_type={type(arguments).__name__}"
        )
    parsed = get_model_tool_schema(tool).model_validate(arguments)
    return parsed.model_dump()


def export_model_tool_json_schema(tool: BaseTool) -> dict[str, Any]:
    """导出发送给模型及请求回放使用的公开 JSON Schema。"""
    tool_call_schema = getattr(tool, "tool_call_schema", None)
    parameters = (
        dict(tool_call_schema)
        if isinstance(tool_call_schema, dict)
        else get_model_tool_schema(tool).model_json_schema()
    )
    if not isinstance(parameters, dict):
        raise TypeError(
            "模型可见工具 JSON Schema 不是 object: "
            f"tool_name={tool.name} tool_type={type(tool).__name__}"
        )
    return parameters
