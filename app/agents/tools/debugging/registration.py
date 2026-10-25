from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from langchain_core.tools import BaseTool, StructuredTool

from .contracts import (
    BreakpointInput,
    CreateDebugConfigurationInput,
    DebugConfigurationIdInput,
    EvaluateExpressionInput,
    LogpointInput,
    RemoveBreakpointInput,
    StartDebuggingInput,
    VariableNamesInput,
    VariableValuesInput,
    _NoArgumentInput,
    _StrictDebugInput,
)

if TYPE_CHECKING:
    from .factory import DebuggingToolFactory


@dataclass(frozen=True, slots=True)
class _ToolRegistration:
    description: str
    input_model: type[_StrictDebugInput]
    action_name: str


_TOOL_REGISTRATIONS: dict[str, _ToolRegistration] = {
    "list_debug_configurations": _ToolRegistration(
        description="首次进入调试时调用：返回会话方案、活动方案、断点和最新状态；不要为了取得控制权而重复启动。",
        input_model=_NoArgumentInput,
        action_name="list_debug_configurations",
    ),
    "create_debug_configuration": _ToolRegistration(
        description="仅在没有匹配方案时创建并激活具名目标程序调试方案；运行中不能直接切换。",
        input_model=CreateDebugConfigurationInput,
        action_name="create_debug_configuration",
    ),
    "activate_debug_configuration": _ToolRegistration(
        description="在没有运行中目标时激活另一套会话调试方案；不会取得或交接控制权。",
        input_model=DebugConfigurationIdInput,
        action_name="activate_debug_configuration",
    ),
    "delete_debug_configuration": _ToolRegistration(
        description="删除当前会话中未运行且不再需要的目标程序调试方案。",
        input_model=DebugConfigurationIdInput,
        action_name="delete_debug_configuration",
    ),
    "start_debugging": _ToolRegistration(
        description=(
            "启动目标 JavaScript；已有活动方案时以方案保存的入口、参数、profile 和断点为准。"
            "返回后先检查 state.status；启动结果还会列出 invalid_breakpoints，但失效断点不会阻止其他代码运行。"
        ),
        input_model=StartDebuggingInput,
        action_name="start_debugging",
    ),
    "stop_debugging": _ToolRegistration(
        description="停止共享的目标源码调试会话；人类和 Agent 不需要先交接控制权。",
        input_model=_NoArgumentInput,
        action_name="stop_debugging",
    ),
    "step_over": _ToolRegistration(
        description="仅在 state.status=paused 时执行一步并跳过函数调用；以返回的实际 state 为准。",
        input_model=_NoArgumentInput,
        action_name="step_over",
    ),
    "step_into": _ToolRegistration(
        description="仅在 state.status=paused 时执行一步并进入函数调用；以返回的实际 state 为准。",
        input_model=_NoArgumentInput,
        action_name="step_into",
    ),
    "step_out": _ToolRegistration(
        description="仅在 state.status=paused 时执行一步并跳出当前函数；以返回的实际 state 为准。",
        input_model=_NoArgumentInput,
        action_name="step_out",
    ),
    "continue_execution": _ToolRegistration(
        description="继续共享的目标进程直到下一个真实断点或程序结束；源码变化导致的失效断点不会阻止继续。",
        input_model=_NoArgumentInput,
        action_name="continue_execution",
    ),
    "pause_execution": _ToolRegistration(
        description="请求暂停共享的目标进程；人类可能已经先暂停或继续，以返回的真实 state 为准。",
        input_model=_NoArgumentInput,
        action_name="pause_execution",
    ),
    "restart_debugging": _ToolRegistration(
        description="按当前活动方案重新启动目标程序；不会自动恢复或重定位失效断点，需显式重新设置。",
        input_model=_NoArgumentInput,
        action_name="restart_debugging",
    ),
    "add_breakpoint": _ToolRegistration(
        description="在当前源码行添加普通、条件或命中次数断点；源码变化后的旧断点必须先检查并重新设置。",
        input_model=BreakpointInput,
        action_name="add_breakpoint",
    ),
    "add_logpoint": _ToolRegistration(
        description="添加只记录日志而不暂停执行的 logpoint；适合观察值而不打断程序。",
        input_model=LogpointInput,
        action_name="add_logpoint",
    ),
    "remove_breakpoint": _ToolRegistration(
        description="移除指定路径和请求行上的断点，包括已失效的断点。",
        input_model=RemoveBreakpointInput,
        action_name="remove_breakpoint",
    ),
    "clear_all_breakpoints": _ToolRegistration(
        description="清除当前调试 session 的全部源码断点，包含失效断点。",
        input_model=_NoArgumentInput,
        action_name="clear_all_breakpoints",
    ),
    "list_breakpoints": _ToolRegistration(
        description="列出当前调试 session 的权威断点状态；重点查看 relocation_status，不要只看 verified。",
        input_model=_NoArgumentInput,
        action_name="list_breakpoints",
    ),
    "list_variable_names": _ToolRegistration(
        description="仅在 state.status=paused 时列出当前暂停位置可见的变量名和类型，不返回变量值。",
        input_model=VariableNamesInput,
        action_name="list_variable_names",
    ),
    "get_variables_values": _ToolRegistration(
        description="仅在 state.status=paused 时读取明确指定的变量值；不要绕过脱敏或读取全部变量。",
        input_model=VariableValuesInput,
        action_name="get_variables_values",
    ),
    "evaluate_expression": _ToolRegistration(
        description="仅在 state.status=paused 时在当前源码上下文求值；可能有副作用，结果进入审计和调试控制台。",
        input_model=EvaluateExpressionInput,
        action_name="evaluate_expression",
    ),
}


def _build_tool(
    factory: DebuggingToolFactory,
    name: str,
    registration: _ToolRegistration,
) -> BaseTool:
    action = cast(
        Callable[..., Awaitable[str]], getattr(factory, registration.action_name)
    )
    return StructuredTool.from_function(
        coroutine=action,
        name=name,
        description=registration.description,
        args_schema=registration.input_model,
    )


def build_debugging_tools(factory: DebuggingToolFactory) -> list[BaseTool]:
    return [
        _build_tool(factory, name, registration)
        for name, registration in _TOOL_REGISTRATIONS.items()
    ]


def build_debugging_tool(
    factory: DebuggingToolFactory,
    tool_name: str,
) -> BaseTool:
    name = tool_name.strip()
    registration = _TOOL_REGISTRATIONS.get(name)
    if registration is None:
        raise ValueError(f"未知源码调试扩展工具: {tool_name}")
    return _build_tool(factory, name, registration)


def debugging_tool_input_json_schema(tool_name: str) -> dict[str, object]:
    """导出扩展目录与信封分发共同使用的调试目标参数 schema。"""
    registration = _TOOL_REGISTRATIONS.get(tool_name)
    if registration is None:
        raise ValueError(f"未知源码调试扩展工具: {tool_name}")
    return registration.input_model.model_json_schema()
