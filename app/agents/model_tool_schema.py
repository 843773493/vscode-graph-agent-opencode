from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
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
    tool_call_schema = getattr(tool, "tool_call_schema", None)
    if isinstance(tool_call_schema, dict):
        Draft202012Validator(tool_call_schema).validate(arguments)
        properties = tool_call_schema.get("properties")
        if not isinstance(properties, dict):
            raise TypeError(
                "工具参数 schema 无法提取公开字段: "
                f"tool_name={tool.name}"
            )
        return {
            field_name: arguments[field_name]
            for field_name in properties
            if field_name in arguments
        }

    parsed = get_model_tool_schema(tool).model_validate(arguments)
    return parsed.model_dump()


def filter_tool_event_injected_arguments(
    tool: BaseTool,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """移除工具事件的后端注入值，保留 Provider 原参数及其校验错误。"""
    if not isinstance(arguments, dict):
        raise TypeError(
            "工具事件参数必须是 object: "
            f"tool_name={tool.name} actual_type={type(arguments).__name__}"
        )

    tool_call_schema = getattr(tool, "tool_call_schema", None)
    if isinstance(tool_call_schema, dict):
        properties = tool_call_schema.get("properties")
        if not isinstance(properties, dict):
            raise TypeError(
                "工具事件参数无法从字典 JSON Schema 提取公开字段: "
                f"tool_name={tool.name}"
            )
    elif not (
        isinstance(tool_call_schema, type)
        and issubclass(tool_call_schema, BaseModel)
    ):
        raise TypeError(
            "工具缺少模型可见的 JSON Schema: "
            f"tool_name={tool.name} tool_type={type(tool).__name__}"
        )
    # 与 LangChain callback 使用同一注入参数过滤规则。观察链不能再次校验、
    # 转换或丢弃原参数，否则 schema 错误调用会失去 canonical claim 身份。
    # 真正的参数校验仍由 BaseTool 执行 owner 完成。
    return tool._filter_injected_args(arguments)


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
