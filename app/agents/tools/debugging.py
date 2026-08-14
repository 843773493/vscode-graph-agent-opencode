from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.agents.custom_tools import CustomToolFactoryContext
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.debug_redaction import (
    REDACTION_NOTICE,
    contains_redaction,
    redact_expression_value,
    redact_free_text,
    redact_variable_value,
)
from app.agents.workspace_tool_paths import WorkspaceToolPathResolver
from app.schemas.public_v2.node_debug import (
    NodeDebugConfigurationCreateRequest,
    NodeDebugStateDTO,
)
from app.services.infrastructure.node_debug_service import NodeDebugService

DebugScope = Literal["local", "global", "all"]
VariableName = Annotated[str, Field(min_length=1)]
_ENDING_DEBUG_TOOLS = frozenset(
    {
        "stop_debugging",
        "continue_execution",
        "step_over",
        "step_into",
        "step_out",
        "restart_debugging",
    }
)


class StartDebuggingInput(BaseModel):
    fileFullPath: str = Field(description="要调试的工作区相对源码路径；不能以 / 开头。")
    workingDirectory: str = Field(
        description="工作区相对调试目录；使用 . 表示 workspace 根目录，不能以 / 开头。"
    )
    testName: str | None = Field(
        default=None,
        description="可选的测试名称；当前 Node Inspector adapter 暂不支持单测试启动。",
    )
    configurationName: str | None = Field(
        default=None,
        description="可选的工作区 debug launch profile 名称。",
    )
    debugConfigurationId: str | None = Field(
        default=None,
        description="可选的当前会话调试方案 ID；省略时使用活动方案。",
    )


class CreateDebugConfigurationInput(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="调试方案显示名。")
    fileFullPath: str = Field(
        description="目标 JavaScript 的工作区相对路径；不能以 / 开头。"
    )
    workingDirectory: str = Field(
        description="目标程序的工作区相对目录；使用 . 表示 workspace 根目录。"
    )
    configurationName: str | None = Field(
        default=None,
        description="可选的工作区 debug launch profile 名称。",
    )
    arguments: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="传给目标程序的启动参数。",
    )


class DebugConfigurationIdInput(BaseModel):
    debugConfigurationId: str = Field(
        min_length=1,
        description="当前会话中的调试方案 ID。",
    )


class BreakpointInput(BaseModel):
    fileFullPath: str = Field(description="源码的工作区相对路径；不能以 / 开头。")
    line: int = Field(ge=1, description="从 1 开始的源码行号。")
    condition: str | None = Field(default=None, description="可选的条件表达式。")
    hitCondition: int | None = Field(
        default=None,
        ge=1,
        description="可选的命中次数；仅在本次目标进程第 N 次到达时暂停。",
    )


class RemoveBreakpointInput(BaseModel):
    fileFullPath: str = Field(description="源码的工作区相对路径；不能以 / 开头。")
    line: int = Field(ge=1, description="从 1 开始的源码行号。")


class LogpointInput(BaseModel):
    fileFullPath: str = Field(description="源码的工作区相对路径；不能以 / 开头。")
    line: int = Field(ge=1, description="从 1 开始的源码行号。")
    logMessage: str = Field(
        min_length=1,
        description="命中时记录的日志消息，使用 {expression} 插入运行时值。",
    )
    condition: str | None = Field(default=None, description="可选的条件表达式。")
    hitCondition: int | None = Field(
        default=None,
        ge=1,
        description="可选的命中次数；仅在本次目标进程第 N 次到达时输出。",
    )


class VariableNamesInput(BaseModel):
    scope: DebugScope | None = Field(
        default=None,
        description="变量范围：local、global 或 all。",
    )


class VariableValuesInput(BaseModel):
    variableNames: list[VariableName] = Field(
        min_length=1,
        max_length=50,
        description="要读取的变量名列表，不支持通配符。",
    )
    scope: DebugScope | None = Field(
        default=None,
        description="变量范围：local、global 或 all。",
    )


class EvaluateExpressionInput(BaseModel):
    expression: str = Field(min_length=1, description="当前暂停上下文中的表达式。")


def _json_result(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _state_payload(state: NodeDebugStateDTO | None) -> dict[str, object] | None:
    if state is None:
        return None
    payload = state.model_dump(mode="json")
    # 这些字段只用于后端把协议请求路由到 Inspector；模型既不需要读取，
    # 也不能把它们作为后续源码调试动作的输入。
    payload.pop("session_id", None)
    payload.pop("pid", None)
    call_stack = payload.get("call_stack")
    if isinstance(call_stack, list):
        for frame in call_stack:
            if isinstance(frame, dict):
                frame.pop("call_frame_id", None)
                # 变量值只能通过 get_variables_values 显式读取，避免任何控制类
                # 工具的通用 state 快照绕过最小权限约束。
                frame["variables"] = []
    last_stopped_frame = payload.get("last_stopped_frame")
    if isinstance(last_stopped_frame, dict):
        last_stopped_frame.pop("call_frame_id", None)
        last_stopped_frame["variables"] = []
    breakpoints = payload.get("breakpoints")
    if isinstance(breakpoints, list):
        for breakpoint in breakpoints:
            if isinstance(breakpoint, dict):
                breakpoint.pop("breakpoint_id", None)
                breakpoint.pop("inspector_id", None)
    actions = payload.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict):
                action.pop("action_id", None)
                action.pop("session_id", None)
                action.pop("tool_call_id", None)
    output = payload.get("output")
    if isinstance(output, list):
        payload["output"] = [
            redact_free_text(item)[0] if isinstance(item, str) else item
            for item in output
        ]
    evaluation = payload.get("last_evaluation")
    if isinstance(evaluation, dict):
        expression = evaluation.get("expression")
        if isinstance(expression, str):
            for field_name in ("value", "description"):
                value = evaluation.get(field_name)
                if value is not None:
                    evaluation[field_name] = redact_expression_value(
                        expression,
                        value,
                    )[0]
    evaluations = payload.get("evaluations")
    if isinstance(evaluations, list):
        for history_evaluation in evaluations:
            if not isinstance(history_evaluation, dict):
                continue
            expression = history_evaluation.get("expression")
            if not isinstance(expression, str):
                continue
            for field_name in ("value", "description"):
                value = history_evaluation.get(field_name)
                if value is not None:
                    history_evaluation[field_name] = redact_expression_value(
                        expression,
                        value,
                    )[0]
    return payload


def _invalid_breakpoint_payload(
    state_payload: dict[str, object] | None,
) -> list[dict[str, object]]:
    if state_payload is None:
        return []
    breakpoints = state_payload.get("breakpoints")
    if not isinstance(breakpoints, list):
        return []
    invalid: list[dict[str, object]] = []
    for breakpoint in breakpoints:
        if not isinstance(breakpoint, dict):
            continue
        status = breakpoint.get("relocation_status")
        if status == "current":
            continue
        invalid.append(
            {
                "path": breakpoint.get("path"),
                "line": breakpoint.get("line"),
                "column": breakpoint.get("column"),
                "original_line": breakpoint.get("original_line"),
                "relocation_status": status,
                "relocation_message": breakpoint.get("relocation_message"),
            }
        )
    return invalid


def _success(
    message: str,
    state: NodeDebugStateDTO | None,
    *,
    include_invalid_breakpoints: bool = False,
) -> str:
    state_payload = _state_payload(state)
    payload: dict[str, object] = {
        "ok": True,
        "message": message,
        "state": state_payload,
    }
    if include_invalid_breakpoints:
        payload["invalid_breakpoints"] = _invalid_breakpoint_payload(state_payload)
    if contains_redaction(payload):
        payload["redaction_notice"] = REDACTION_NOTICE
    return _json_result(payload)


def _failure(
    code: str,
    message: str,
    state: NodeDebugStateDTO | None = None,
    *,
    include_invalid_breakpoints: bool = False,
) -> str:
    state_payload = _state_payload(state)
    payload: dict[str, object] = {
        "ok": False,
        "error": {"code": code, "message": message},
        "state": state_payload,
    }
    if include_invalid_breakpoints:
        payload["invalid_breakpoints"] = _invalid_breakpoint_payload(state_payload)
    if contains_redaction(payload):
        payload["redaction_notice"] = REDACTION_NOTICE
    return _json_result(payload)


class DebuggingToolFactory:
    """将 DebugMCP 风格工具绑定到当前 Agent session。"""

    def __init__(
        self,
        *,
        session_id: str,
        workspace_root: Path,
        node_debug_service: NodeDebugService,
        invocation_context: ToolInvocationContext,
    ) -> None:
        self._session_id = session_id
        self._path_resolver = WorkspaceToolPathResolver(workspace_root)
        self._node_debug_service = node_debug_service
        self._invocation_context = invocation_context

    def build(self) -> list[BaseTool]:
        return [
            self._no_argument_tool(
                "list_debug_configurations",
                "首次进入调试时调用：返回会话方案、活动方案、断点和最新状态；不要为了取得控制权而重复启动。",
                self.list_debug_configurations,
            ),
            StructuredTool.from_function(
                coroutine=self.create_debug_configuration,
                name="create_debug_configuration",
                description="仅在没有匹配方案时创建并激活具名目标程序调试方案；运行中不能直接切换。",
                args_schema=CreateDebugConfigurationInput,
            ),
            StructuredTool.from_function(
                coroutine=self.activate_debug_configuration,
                name="activate_debug_configuration",
                description="在没有运行中目标时激活另一套会话调试方案；不会取得或交接控制权。",
                args_schema=DebugConfigurationIdInput,
            ),
            StructuredTool.from_function(
                coroutine=self.delete_debug_configuration,
                name="delete_debug_configuration",
                description="删除当前会话中未运行且不再需要的目标程序调试方案。",
                args_schema=DebugConfigurationIdInput,
            ),
            StructuredTool.from_function(
                coroutine=self.start_debugging,
                name="start_debugging",
                description=(
                    "启动目标 JavaScript；已有活动方案时以方案保存的入口、参数、profile 和断点为准。"
                    "返回后先检查 state.status；启动结果还会列出 invalid_breakpoints，但失效断点不会阻止其他代码运行。"
                ),
                args_schema=StartDebuggingInput,
            ),
            self._no_argument_tool(
                "stop_debugging",
                "停止共享的目标源码调试会话；人类和 Agent 不需要先交接控制权。",
                self.stop_debugging,
            ),
            self._no_argument_tool(
                "step_over",
                "仅在 state.status=paused 时执行一步并跳过函数调用；以返回的实际 state 为准。",
                self.step_over,
            ),
            self._no_argument_tool(
                "step_into",
                "仅在 state.status=paused 时执行一步并进入函数调用；以返回的实际 state 为准。",
                self.step_into,
            ),
            self._no_argument_tool(
                "step_out",
                "仅在 state.status=paused 时执行一步并跳出当前函数；以返回的实际 state 为准。",
                self.step_out,
            ),
            self._no_argument_tool(
                "continue_execution",
                "继续共享的目标进程直到下一个真实断点或程序结束；源码变化导致的失效断点不会阻止继续。",
                self.continue_execution,
            ),
            self._no_argument_tool(
                "pause_execution",
                "请求暂停共享的目标进程；人类可能已经先暂停或继续，以返回的真实 state 为准。",
                self.pause_execution,
            ),
            self._no_argument_tool(
                "restart_debugging",
                "按当前活动方案重新启动目标程序；不会自动恢复或重定位失效断点，需显式重新设置。",
                self.restart_debugging,
            ),
            StructuredTool.from_function(
                coroutine=self.add_breakpoint,
                name="add_breakpoint",
                description="在当前源码行添加普通、条件或命中次数断点；源码变化后的旧断点必须先检查并重新设置。",
                args_schema=BreakpointInput,
            ),
            StructuredTool.from_function(
                coroutine=self.add_logpoint,
                name="add_logpoint",
                description="添加只记录日志而不暂停执行的 logpoint；适合观察值而不打断程序。",
                args_schema=LogpointInput,
            ),
            StructuredTool.from_function(
                coroutine=self.remove_breakpoint,
                name="remove_breakpoint",
                description="移除指定路径和请求行上的断点，包括已失效的断点。",
                args_schema=RemoveBreakpointInput,
            ),
            self._no_argument_tool(
                "clear_all_breakpoints",
                "清除当前调试 session 的全部源码断点，包含失效断点。",
                self.clear_all_breakpoints,
            ),
            self._no_argument_tool(
                "list_breakpoints",
                "列出当前调试 session 的权威断点状态；重点查看 relocation_status，不要只看 verified。",
                self.list_breakpoints,
            ),
            StructuredTool.from_function(
                coroutine=self.list_variable_names,
                name="list_variable_names",
                description="仅在 state.status=paused 时列出当前暂停位置可见的变量名和类型，不返回变量值。",
                args_schema=VariableNamesInput,
            ),
            StructuredTool.from_function(
                coroutine=self.get_variables_values,
                name="get_variables_values",
                description="仅在 state.status=paused 时读取明确指定的变量值；不要绕过脱敏或读取全部变量。",
                args_schema=VariableValuesInput,
            ),
            StructuredTool.from_function(
                coroutine=self.evaluate_expression,
                name="evaluate_expression",
                description="仅在 state.status=paused 时在当前源码上下文求值；可能有副作用，结果进入审计和调试控制台。",
                args_schema=EvaluateExpressionInput,
            ),
        ]

    @staticmethod
    def _no_argument_tool(
        name: str,
        description: str,
        coroutine: Callable[[], Awaitable[str]],
    ) -> BaseTool:
        return StructuredTool.from_function(
            coroutine=coroutine,
            name=name,
            description=description,
        )

    async def _invoke(
        self,
        tool_name: str,
        operation: Callable[[str], Awaitable[NodeDebugStateDTO]],
    ) -> str:
        tool_call_id = self._tool_call_id()
        try:
            state = await operation(tool_call_id)
            await self._record_action(
                tool_name,
                "success",
                f"工具调用成功: {tool_name}",
                tool_call_id=tool_call_id,
            )
            state = await self._safe_state() or state
            return _success(
                f"{tool_name} 执行成功",
                state,
                include_invalid_breakpoints=(
                    tool_name == "start_debugging"
                    or (
                        tool_name in _ENDING_DEBUG_TOOLS
                        and state.status in {"exited", "failed"}
                    )
                ),
            )
        except Exception as error:  # noqa: BLE001 - 工具协议必须把运行时错误转成结构化结果
            state = await self._safe_state()
            await self._record_action(
                tool_name,
                "error",
                str(error),
                tool_call_id=tool_call_id,
            )
            state = await self._safe_state() or state
            return _failure(
                self._error_code(error),
                str(error),
                state,
                include_invalid_breakpoints=(
                    tool_name == "start_debugging"
                    or (
                        tool_name in _ENDING_DEBUG_TOOLS
                        and state is not None
                        and state.status in {"exited", "failed"}
                    )
                ),
            )

    async def _record_action(
        self,
        tool_name: str,
        result: Literal["success", "error"],
        message: str,
        *,
        tool_call_id: str | None = None,
    ) -> None:
        resolved_tool_call_id = tool_call_id or self._tool_call_id()
        try:
            await self._node_debug_service.record_tool_action(
                session_id=self._session_id,
                tool_name=tool_name,
                tool_call_id=resolved_tool_call_id,
                result=result,
                message=message,
            )
        except RuntimeError:
            # 启动前的断点没有 runtime，断点本身仍由 session pending 状态承载。
            if result == "error":
                return

    def _tool_call_id(self) -> str:
        try:
            return self._invocation_context.require_tool_call_id()
        except RuntimeError:
            # 直接调用 factory 的后端测试没有 Agent middleware；仍保留明确身份。
            return "direct-backend-test"

    async def _safe_state(self) -> NodeDebugStateDTO | None:
        return await self._node_debug_service.get_state(self._session_id)

    async def _failure_from_error(self, tool_name: str, error: Exception) -> str:
        state = await self._safe_state()
        await self._record_action(tool_name, "error", str(error))
        state = await self._safe_state() or state
        return _failure(
            self._error_code(error),
            str(error),
            state,
            include_invalid_breakpoints=(
                tool_name == "start_debugging"
                or (
                    tool_name in _ENDING_DEBUG_TOOLS
                    and state is not None
                    and state.status in {"exited", "failed"}
                )
            ),
        )

    @staticmethod
    def _error_code(error: Exception) -> str:
        message = str(error)
        if "不支持" in message:
            return "UNSUPPORTED_DEBUG_FEATURE"
        if "没有活动" in message or "会话不存在" in message:
            return "NO_ACTIVE_DEBUG_SESSION"
        if "暂停" in message:
            return "DEBUG_CONTEXT_REQUIRED"
        if isinstance(error, (ValueError, TypeError)):
            return "INVALID_DEBUG_ARGUMENT"
        return "DEBUG_TOOL_FAILED"

    def _relative_workspace_path(self, raw_path: str, field_name: str) -> str:
        return self._path_resolver.workspace_relative_path(
            raw_path,
            field_name=field_name,
        )

    async def start_debugging(
        self,
        fileFullPath: str,
        workingDirectory: str,
        testName: str | None = None,
        configurationName: str | None = None,
        debugConfigurationId: str | None = None,
    ) -> str:
        if testName:
            message = "当前 Node Inspector adapter 暂不支持通过 testName 启动单个测试。"
            await self._record_action("start_debugging", "error", message)
            return _failure(
                "UNSUPPORTED_TEST_TARGET",
                message,
                await self._safe_state(),
                include_invalid_breakpoints=True,
            )
        try:
            path = self._relative_workspace_path(fileFullPath, "fileFullPath")
            working_directory = self._relative_workspace_path(
                workingDirectory,
                "workingDirectory",
            )
            return await self._invoke(
                "start_debugging",
                lambda tool_call_id: self._node_debug_service.start(
                    session_id=self._session_id,
                    configuration_id=debugConfigurationId,
                    path=path,
                    args=[],
                    breakpoints=[],
                    launch_profile_name=configurationName,
                    working_directory=working_directory,
                    actor="ai",
                    tool_name="start_debugging",
                    tool_call_id=tool_call_id,
                ),
            )
        except Exception as error:  # noqa: BLE001 - 参数错误也必须返回可审计的工具结果
            return await self._failure_from_error("start_debugging", error)

    async def list_debug_configurations(self) -> str:
        return await self._invoke(
            "list_debug_configurations",
            lambda _tool_call_id: self._node_debug_service.get_state(self._session_id),
        )

    async def create_debug_configuration(
        self,
        name: str,
        fileFullPath: str,
        workingDirectory: str,
        configurationName: str | None = None,
        arguments: list[str] | None = None,
    ) -> str:
        try:
            path = self._relative_workspace_path(fileFullPath, "fileFullPath")
            working_directory = self._relative_workspace_path(
                workingDirectory,
                "workingDirectory",
            )
            return await self._invoke(
                "create_debug_configuration",
                lambda tool_call_id: self._node_debug_service.create_configuration(
                    NodeDebugConfigurationCreateRequest(
                        session_id=self._session_id,
                        name=name,
                        script_path=path,
                        working_directory=working_directory,
                        launch_profile_name=configurationName,
                        args=arguments or [],
                        activate=True,
                    ),
                    actor="ai",
                    tool_name="create_debug_configuration",
                    tool_call_id=tool_call_id,
                ),
            )
        except Exception as error:  # noqa: BLE001 - 参数错误必须返回结构化结果
            return await self._failure_from_error(
                "create_debug_configuration",
                error,
            )

    async def activate_debug_configuration(
        self,
        debugConfigurationId: str,
    ) -> str:
        return await self._invoke(
            "activate_debug_configuration",
            lambda tool_call_id: self._node_debug_service.activate_configuration(
                self._session_id,
                debugConfigurationId,
                actor="ai",
                tool_name="activate_debug_configuration",
                tool_call_id=tool_call_id,
            ),
        )

    async def delete_debug_configuration(
        self,
        debugConfigurationId: str,
    ) -> str:
        return await self._invoke(
            "delete_debug_configuration",
            lambda tool_call_id: self._node_debug_service.delete_configuration(
                self._session_id,
                debugConfigurationId,
                actor="ai",
                tool_name="delete_debug_configuration",
                tool_call_id=tool_call_id,
            ),
        )

    async def stop_debugging(self) -> str:
        return await self._invoke(
            "stop_debugging",
            lambda tool_call_id: self._node_debug_service.apply_action(
                session_id=self._session_id,
                action="stop",
                params={},
                actor="ai",
                tool_name="stop_debugging",
                tool_call_id=tool_call_id,
            ),
        )

    async def restart_debugging(self) -> str:
        return await self._invoke(
            "restart_debugging",
            lambda tool_call_id: self._node_debug_service.restart(
                self._session_id,
                actor="ai",
                tool_name="restart_debugging",
                tool_call_id=tool_call_id,
            ),
        )

    async def continue_execution(self) -> str:
        return await self._control("continue_execution", "continue")

    async def pause_execution(self) -> str:
        return await self._control("pause_execution", "pause")

    async def step_over(self) -> str:
        return await self._control("step_over", "step_over")

    async def step_into(self) -> str:
        return await self._control("step_into", "step_into")

    async def step_out(self) -> str:
        return await self._control("step_out", "step_out")

    async def _control(self, tool_name: str, action: str) -> str:
        return await self._invoke(
            tool_name,
            lambda tool_call_id: self._node_debug_service.apply_action(
                session_id=self._session_id,
                action=action,  # type: ignore[arg-type]
                params={},
                actor="ai",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            ),
        )

    async def add_breakpoint(
        self,
        fileFullPath: str,
        line: int,
        condition: str | None = None,
        hitCondition: int | None = None,
    ) -> str:
        try:
            path = self._relative_workspace_path(fileFullPath, "fileFullPath")
            return await self._invoke(
                "add_breakpoint",
                lambda tool_call_id: self._node_debug_service.apply_action(
                    session_id=self._session_id,
                    action="set_breakpoint",
                    params={
                        "path": path,
                        "line": line,
                        "condition": condition,
                        "hit_condition": hitCondition,
                    },
                    actor="ai",
                    tool_name="add_breakpoint",
                    tool_call_id=tool_call_id,
                ),
            )
        except Exception as error:  # noqa: BLE001 - 参数错误也必须返回可审计的工具结果
            return await self._failure_from_error("add_breakpoint", error)

    async def add_logpoint(
        self,
        fileFullPath: str,
        line: int,
        logMessage: str,
        condition: str | None = None,
        hitCondition: int | None = None,
    ) -> str:
        try:
            path = self._relative_workspace_path(fileFullPath, "fileFullPath")
            return await self._invoke(
                "add_logpoint",
                lambda tool_call_id: self._node_debug_service.apply_action(
                    session_id=self._session_id,
                    action="set_breakpoint",
                    params={
                        "path": path,
                        "line": line,
                        "condition": condition,
                        "hit_condition": hitCondition,
                        "log_message": logMessage,
                    },
                    actor="ai",
                    tool_name="add_logpoint",
                    tool_call_id=tool_call_id,
                ),
            )
        except Exception as error:  # noqa: BLE001 - 参数错误也必须返回可审计的工具结果
            return await self._failure_from_error("add_logpoint", error)

    async def remove_breakpoint(self, fileFullPath: str, line: int) -> str:
        try:
            path = self._relative_workspace_path(fileFullPath, "fileFullPath")
            state = await self._node_debug_service.get_state(self._session_id)
            breakpoint = next(
                (
                    item
                    for item in state.breakpoints
                    if item.path == path and item.line == line
                ),
                None,
            )
            if breakpoint is None:
                raise ValueError(f"源码断点不存在: {path}:{line}")
            return await self._invoke(
                "remove_breakpoint",
                lambda tool_call_id: self._node_debug_service.apply_action(
                    session_id=self._session_id,
                    action="clear_breakpoint",
                    params={"breakpoint_id": breakpoint.breakpoint_id},
                    actor="ai",
                    tool_name="remove_breakpoint",
                    tool_call_id=tool_call_id,
                ),
            )
        except Exception as error:  # noqa: BLE001 - 参数错误也必须返回可审计的工具结果
            return await self._failure_from_error("remove_breakpoint", error)

    async def clear_all_breakpoints(self) -> str:
        return await self._invoke(
            "clear_all_breakpoints",
            lambda tool_call_id: self._node_debug_service.clear_all_breakpoints(
                self._session_id,
                actor="ai",
                tool_name="clear_all_breakpoints",
                tool_call_id=tool_call_id,
            ),
        )

    async def list_breakpoints(self) -> str:
        return await self._invoke(
            "list_breakpoints",
            lambda _tool_call_id: self._node_debug_service.get_state(self._session_id),
        )

    async def list_variable_names(self, scope: DebugScope | None = None) -> str:
        selected_scope = scope or "all"
        try:
            variables = await self._node_debug_service.get_variables(
                session_id=self._session_id,
                scope=selected_scope,
            )
            state = await self._safe_state()
            await self._record_action(
                "list_variable_names",
                "success",
                "已列出暂停上下文变量名",
            )
            state = await self._safe_state() or state
            return _json_result(
                {
                    "ok": True,
                    "message": "已列出变量名",
                    "variables": [
                        {"name": item.name, "type": item.type, "scope": item.scope}
                        for item in variables
                    ],
                    "state": _state_payload(state),
                }
            )
        except Exception as error:  # noqa: BLE001 - 调试适配器错误必须原样返回给 Agent
            return await self._failure_from_error("list_variable_names", error)

    async def get_variables_values(
        self,
        variableNames: list[str],
        scope: DebugScope | None = None,
    ) -> str:
        selected_scope = scope or "all"
        try:
            variables = await self._node_debug_service.get_variables(
                session_id=self._session_id,
                variable_names=variableNames,
                scope=selected_scope,
            )
            state = await self._safe_state()
            await self._record_action(
                "get_variables_values",
                "success",
                "已读取暂停上下文变量值",
            )
            state = await self._safe_state() or state
            redacted = False
            variable_payloads: list[dict[str, object]] = []
            for item in variables:
                variable_payload = item.model_dump(mode="json")
                variable_payload.pop("object_id", None)
                value, item_redacted = redact_variable_value(item.name, item.value)
                variable_payload["value"] = value
                redacted = redacted or item_redacted
                variable_payloads.append(variable_payload)
            payload: dict[str, object] = {
                "ok": True,
                "message": "已读取变量值",
                "variables": variable_payloads,
                "state": _state_payload(state),
            }
            if redacted or contains_redaction(payload):
                payload["redaction_notice"] = REDACTION_NOTICE
            return _json_result(payload)
        except Exception as error:  # noqa: BLE001 - 调试适配器错误必须原样返回给 Agent
            return await self._failure_from_error("get_variables_values", error)

    async def evaluate_expression(self, expression: str) -> str:
        return await self._invoke(
            "evaluate_expression",
            lambda tool_call_id: self._node_debug_service.apply_action(
                session_id=self._session_id,
                action="evaluate",
                params={"expression": expression},
                actor="ai",
                tool_name="evaluate_expression",
                tool_call_id=tool_call_id,
            ),
        )


def create_debugging_tools(
    *,
    session_id: str,
    workspace_root: Path,
    node_debug_service: NodeDebugService,
    invocation_context: ToolInvocationContext,
) -> list[BaseTool]:
    return DebuggingToolFactory(
        session_id=session_id,
        workspace_root=workspace_root,
        node_debug_service=node_debug_service,
        invocation_context=invocation_context,
    ).build()


def create_debugging_tool(context: CustomToolFactoryContext) -> BaseTool:
    """创建一个只能通过 invoke_custom_tool 调用的源码调试扩展工具。"""
    node_debug_service = context.node_debug_service
    if node_debug_service is None:
        raise RuntimeError("调试扩展工具需要由 Agent runtime 注入 NodeDebugService")
    raw_tool_name = context.tool_options.get("tool_name")
    if not isinstance(raw_tool_name, str) or not raw_tool_name.strip():
        raise ValueError("调试扩展工具 options.tool_name 必须是非空字符串")
    tools = DebuggingToolFactory(
        session_id=context.session_id,
        workspace_root=context.workspace_root,
        node_debug_service=node_debug_service,
        invocation_context=context.invocation_context,
    ).build()
    for tool in tools:
        if tool.name == raw_tool_name.strip():
            return tool
    raise ValueError(f"未知源码调试扩展工具: {raw_tool_name}")


__all__ = ["create_debugging_tool", "create_debugging_tools"]
