from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.agents.model_tool_schema import validate_model_tool_arguments
from app.agents.tool_identity import CUSTOM_TOOL_INVOKER_NAME


class CustomToolInvocationInput(BaseModel):
    """固定扩展工具入口参数。"""

    tool_name: str = Field(
        description="要调用的目标扩展工具名称，例如 test_tool_2。",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="传给目标扩展工具的参数对象。目标工具不需要参数时传空对象 {}。",
    )


def _normalize_arguments(arguments: dict[str, Any] | None) -> dict[str, Any]:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise TypeError(f"arguments 必须是 object，实际类型: {type(arguments).__name__}")
    return arguments


def _validate_target_arguments(
    target_tool: BaseTool,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized_arguments = _normalize_arguments(arguments)
    return validate_model_tool_arguments(target_tool, normalized_arguments)


async def _invoke_target_tool_without_nested_callbacks(
    target_tool: BaseTool,
    arguments: dict[str, Any] | None,
) -> Any:
    call_arguments = _validate_target_arguments(target_tool, arguments)

    coroutine = getattr(target_tool, "coroutine", None)
    if callable(coroutine):
        return await coroutine(**call_arguments)

    func = getattr(target_tool, "func", None)
    if callable(func):
        result = func(**call_arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    # TODO: 支持没有暴露 func/coroutine 的自定义 BaseTool 时，仍会经过 LangChain 回调。
    return await target_tool.ainvoke(call_arguments)


def create_custom_tool_invoker_tool(custom_tools: Sequence[BaseTool]) -> BaseTool:
    """创建固定扩展工具入口，通过参数分发到工作区配置的自定义工具。"""
    tools_by_name: dict[str, BaseTool] = {}
    for custom_tool in custom_tools:
        if custom_tool.name in tools_by_name:
            raise ValueError(f"重复的扩展工具: {custom_tool.name}")
        tools_by_name[custom_tool.name] = custom_tool

    async def invoke_custom_tool(
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        resolved_tool_name = tool_name.strip()
        if not resolved_tool_name:
            raise ValueError("tool_name 不能为空")

        target_tool = tools_by_name.get(resolved_tool_name)
        if target_tool is None:
            available = ", ".join(sorted(tools_by_name)) or "无"
            raise ValueError(
                f"未知扩展工具: {resolved_tool_name}。当前可用扩展工具: {available}"
            )

        return await _invoke_target_tool_without_nested_callbacks(
            target_tool,
            arguments,
        )

    description = (
        "调用工作区配置的扩展工具。"
        "当需要执行未直接出现在 tools 列表中的目标扩展工具时，必须真实调用本工具，"
        "不要在正文里复述或伪造调用。"
        "目标工具名称和参数来自当前工作区的 AGENTS.md、SKILL.md 或普通说明文档。"
        '调用参数格式: {"tool_name": "<目标工具名>", "arguments": {}}。'
    )
    return StructuredTool.from_function(
        coroutine=invoke_custom_tool,
        name=CUSTOM_TOOL_INVOKER_NAME,
        description=description,
        args_schema=CustomToolInvocationInput,
        handle_tool_error=True,
    )
