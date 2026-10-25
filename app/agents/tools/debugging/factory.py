from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from langchain_core.tools import BaseTool

from app.agents.custom_tools import CustomToolFactoryContext
from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.agents.tools.debug_redaction import redact_variable_value
from app.schemas.internal_v2.node_debug import (
    ExtensionCatalogBindingAuditDTO,
    NodeDebugClearBreakpointActionRequest,
    NodeDebugClearBreakpointParams,
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugControlActionRequest,
    NodeDebugEvaluateActionRequest,
    NodeDebugEvaluateParams,
    NodeDebugSetBreakpointActionRequest,
    NodeDebugSetBreakpointParams,
    NodeDebugStateDTO,
)
from app.services.infrastructure.node_debug.service import NodeDebugService

from .contracts import DebugScope
from .payloads import (
    REDACTION_NOTICE,
    _failure,
    _include_invalid_breakpoints,
    _json_result,
    _state_payload,
    _success,
    contains_redaction,
)
from .registration import build_debugging_tool, build_debugging_tools


@dataclass(frozen=True, slots=True)
class _LaunchConfigurationSelection:
    """启动前的方案选择结果：命中方案或明确的“显式 ID 不存在”。"""

    configuration: NodeDebugConfigurationDTO | None = None
    missing_configuration_id: str | None = None


class DebuggingToolFactory:
    """把 DebugMCP 风格目标绑定到受信 (session_id, thread_id) 归属。

    工具只通过固定扩展入口 ``invoke_extension_tool`` 暴露给模型；这里的
    ``build()`` 结果只供信封分发和直接后端测试调用。
    """

    def __init__(
        self,
        *,
        session_id: str,
        node_debug_service: NodeDebugService,
        invocation_context: ToolInvocationContext,
        thread_binding: ThreadRuntimeBinding | None = None,
    ) -> None:
        self._node_debug_service = node_debug_service
        self._invocation_context = invocation_context
        self._thread_binding = thread_binding or invocation_context.thread_binding

    @property
    def _owner(self) -> tuple[str, str]:
        """受信 (session_id, thread_id)；模型无法覆盖它。"""
        if self._thread_binding is None:
            raise RuntimeError(
                "Node 调试工具调用缺少 canonical SessionThread 运行时绑定"
            )
        return self._thread_binding.session_id, self._thread_binding.thread_id

    def build(self) -> list[BaseTool]:
        return build_debugging_tools(self)

    def build_tool(self, tool_name: str) -> BaseTool:
        return build_debugging_tool(self, tool_name)

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
                include_invalid_breakpoints=_include_invalid_breakpoints(
                    tool_name,
                    state,
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
                include_invalid_breakpoints=_include_invalid_breakpoints(
                    tool_name,
                    state,
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
        session_id, thread_id = self._owner
        resolved_tool_call_id = tool_call_id or self._tool_call_id()
        binding = self._invocation_context.current_extension_catalog_binding()
        binding_audit: ExtensionCatalogBindingAuditDTO | None = None
        if binding is not None:
            target = binding.resolve(tool_name)
            binding_audit = ExtensionCatalogBindingAuditDTO(
                binding_id=binding.binding_id,
                binding_hash=binding.binding_hash,
                catalog_revision=binding.catalog_revision,
                generation=binding.generation,
                provider_binding_identity=binding.provider_binding_identity,
                target_id=target.target_id,
                target_schema_hash=target.schema_hash,
            )
        try:
            await self._node_debug_service.record_tool_action(
                session_id=session_id,
                thread_id=thread_id,
                tool_name=tool_name,
                tool_call_id=resolved_tool_call_id,
                extension_catalog_binding=binding_audit,
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
        session_id, thread_id = self._owner
        return await self._node_debug_service.get_state(session_id, thread_id)

    async def _failure_from_error(self, tool_name: str, error: Exception) -> str:
        state = await self._safe_state()
        await self._record_action(tool_name, "error", str(error))
        state = await self._safe_state() or state
        return _failure(
            self._error_code(error),
            str(error),
            state,
            include_invalid_breakpoints=_include_invalid_breakpoints(
                tool_name,
                state,
            ),
        )

    @staticmethod
    def _error_code(error: Exception) -> str:
        message = str(error)
        if "调试方案不存在" in message:
            return "debug_configuration_not_found"
        if "不支持" in message:
            return "UNSUPPORTED_DEBUG_FEATURE"
        if "没有活动" in message or "会话不存在" in message:
            return "NO_ACTIVE_DEBUG_SESSION"
        if "暂停" in message:
            return "DEBUG_CONTEXT_REQUIRED"
        if isinstance(error, FileNotFoundError):
            return "INVALID_DEBUG_ARGUMENT"
        if isinstance(error, (ValueError, TypeError)):
            return "INVALID_DEBUG_ARGUMENT"
        return "DEBUG_TOOL_FAILED"

    def _canonical_directory(self, raw_path: str | None, field_name: str) -> str:
        """把目录规范化为与方案存储一致的相对形式（workspace 根为 ""）。"""
        normalized = self._node_debug_service.workspace_relative_path(
            raw_path or ".", field_name
        )
        return "" if normalized == "." else normalized

    async def _select_launch_configuration(
        self,
        *,
        session_id: str,
        thread_id: str,
        state: NodeDebugStateDTO,
        configuration_id: str | None,
    ) -> _LaunchConfigurationSelection:
        """按 显式 ID → 当前 thread 活动方案 → 无方案 的顺序解析启动方案。

        显式 ID 只在受信 thread 内解析；ID 不存在时立即返回 not-found 选择，
        绝不先激活方案或触碰任何进程。
        """
        selected_id = configuration_id or state.active_configuration_id
        if selected_id is None:
            return _LaunchConfigurationSelection()
        configurations = self._node_debug_service.list_configurations(
            session_id,
            thread_id,
        )
        for configuration in configurations:
            if configuration.configuration_id == selected_id:
                return _LaunchConfigurationSelection(configuration=configuration)
        return _LaunchConfigurationSelection(missing_configuration_id=selected_id)

    def _launch_parameter_conflicts(
        self,
        configuration: NodeDebugConfigurationDTO,
        *,
        path: str,
        working_directory: str,
        launch_profile_name: str | None,
    ) -> dict[str, dict[str, str]]:
        """比较启动请求与选中方案；返回按字段聚合的冲突明细。

        两个必填路径必须与方案的有效入口/工作目录规范化相等；显式 profile 必须与
        方案解析结果一致。调用方必须在激活方案、停止旧进程和启动新进程之前检查。
        """
        if configuration.script_path is None:
            raise ValueError(f"调试方案没有目标文件: {configuration.name}")
        conflicts: dict[str, dict[str, str]] = {}
        expected_path = self._node_debug_service.workspace_relative_path(
            configuration.script_path,
            "script_path",
        )
        if path != expected_path:
            conflicts["fileFullPath"] = {
                "provided": path,
                "expected": expected_path,
            }
        expected_directory = self._canonical_directory(
            configuration.working_directory,
            "working_directory",
        )
        if working_directory != expected_directory:
            conflicts["workingDirectory"] = {
                "provided": working_directory,
                "expected": expected_directory,
            }
        if launch_profile_name is not None:
            expected_profile = self._node_debug_service.resolve_launch_profile_name(
                configuration.launch_profile_name
            )
            if launch_profile_name != expected_profile:
                conflicts["configurationName"] = {
                    "provided": launch_profile_name,
                    "expected": expected_profile,
                }
        return conflicts

    async def start_debugging(
        self,
        fileFullPath: str,
        workingDirectory: str,
        testName: str | None = None,
        configurationName: str | None = None,
        debugConfigurationId: str | None = None,
    ) -> str:
        session_id, thread_id = self._owner
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
            state = await self._node_debug_service.get_state(session_id, thread_id)
            selection = await self._select_launch_configuration(
                session_id=session_id,
                thread_id=thread_id,
                state=state,
                configuration_id=debugConfigurationId,
            )
            if selection.missing_configuration_id is not None:
                # 启动前拒绝不写调试动作时间线：此时没有方案被激活、没有进程被触碰，
                # 失败事实由 Agent 工具轨迹与这里的结构化错误承载。
                return _failure(
                    "debug_configuration_not_found",
                    "显式指定的调试方案不存在于当前 SessionThread: "
                    f"{selection.missing_configuration_id}",
                    state,
                    include_invalid_breakpoints=True,
                    error_details={
                        "configuration_id": selection.missing_configuration_id,
                        "available_configuration_ids": [
                            item.configuration_id for item in state.configurations
                        ],
                    },
                )
            selected_configuration = selection.configuration
            path = self._node_debug_service.workspace_relative_path(
                fileFullPath, "fileFullPath"
            )
            working_directory = self._canonical_directory(
                workingDirectory,
                "workingDirectory",
            )
            if selected_configuration is not None:
                conflicts = self._launch_parameter_conflicts(
                    selected_configuration,
                    path=path,
                    working_directory=working_directory,
                    launch_profile_name=configurationName,
                )
                if conflicts:
                    return _failure(
                        "debug_launch_parameter_conflict",
                        "启动参数与选中调试方案不一致；已拒绝在启动前覆盖方案: "
                        f"{', '.join(sorted(conflicts))}",
                        state,
                        include_invalid_breakpoints=True,
                        error_details={
                            "configuration_id": selected_configuration.configuration_id,
                            "configuration_name": selected_configuration.name,
                            "fields": sorted(conflicts),
                            "conflicts": conflicts,
                        },
                    )
            return await self._invoke(
                "start_debugging",
                lambda tool_call_id: self._node_debug_service.start(
                    session_id=session_id,
                    thread_id=thread_id,
                    configuration_id=(
                        selected_configuration.configuration_id
                        if selected_configuration is not None
                        else None
                    ),
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
        session_id, thread_id = self._owner
        return await self._invoke(
            "list_debug_configurations",
            lambda _tool_call_id: self._node_debug_service.get_state(
                session_id,
                thread_id,
            ),
        )

    async def create_debug_configuration(
        self,
        name: str,
        fileFullPath: str,
        workingDirectory: str,
        configurationName: str | None = None,
        arguments: list[str] | None = None,
    ) -> str:
        session_id, thread_id = self._owner
        try:
            path = self._node_debug_service.workspace_relative_path(
                fileFullPath, "fileFullPath"
            )
            working_directory = self._canonical_directory(
                workingDirectory,
                "workingDirectory",
            )
            return await self._invoke(
                "create_debug_configuration",
                lambda tool_call_id: self._node_debug_service.create_configuration(
                    NodeDebugConfigurationCreateRequest(
                        session_id=session_id,
                        thread_id=thread_id,
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
        session_id, thread_id = self._owner
        return await self._invoke(
            "activate_debug_configuration",
            lambda tool_call_id: self._node_debug_service.activate_configuration(
                session_id,
                debugConfigurationId,
                thread_id=thread_id,
                actor="ai",
                tool_name="activate_debug_configuration",
                tool_call_id=tool_call_id,
            ),
        )

    async def delete_debug_configuration(
        self,
        debugConfigurationId: str,
    ) -> str:
        session_id, thread_id = self._owner
        return await self._invoke(
            "delete_debug_configuration",
            lambda tool_call_id: self._node_debug_service.delete_configuration(
                session_id,
                debugConfigurationId,
                thread_id=thread_id,
                actor="ai",
                tool_name="delete_debug_configuration",
                tool_call_id=tool_call_id,
            ),
        )

    async def stop_debugging(self) -> str:
        session_id, thread_id = self._owner
        return await self._invoke(
            "stop_debugging",
            lambda tool_call_id: self._node_debug_service.apply_action(
                command=NodeDebugControlActionRequest(
                    session_id=session_id,
                    thread_id=thread_id,
                    action="stop",
                ),
                actor="ai",
                tool_name="stop_debugging",
                tool_call_id=tool_call_id,
            ),
        )

    async def restart_debugging(self) -> str:
        session_id, thread_id = self._owner
        return await self._invoke(
            "restart_debugging",
            lambda tool_call_id: self._node_debug_service.restart(
                session_id,
                thread_id=thread_id,
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

    async def _control(
        self,
        tool_name: str,
        action: Literal["continue", "pause", "step_over", "step_into", "step_out"],
    ) -> str:
        session_id, thread_id = self._owner
        return await self._invoke(
            tool_name,
            lambda tool_call_id: self._node_debug_service.apply_action(
                command=NodeDebugControlActionRequest(
                    session_id=session_id,
                    thread_id=thread_id,
                    action=action,
                ),
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
        session_id, thread_id = self._owner
        try:
            path = self._node_debug_service.workspace_relative_path(
                fileFullPath, "fileFullPath"
            )
            return await self._invoke(
                "add_breakpoint",
                lambda tool_call_id: self._node_debug_service.apply_action(
                    command=NodeDebugSetBreakpointActionRequest(
                        session_id=session_id,
                        thread_id=thread_id,
                        action="set_breakpoint",
                        params=NodeDebugSetBreakpointParams(
                            path=path,
                            line=line,
                            condition=condition,
                            hit_condition=hitCondition,
                        ),
                    ),
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
        session_id, thread_id = self._owner
        try:
            path = self._node_debug_service.workspace_relative_path(
                fileFullPath, "fileFullPath"
            )
            return await self._invoke(
                "add_logpoint",
                lambda tool_call_id: self._node_debug_service.apply_action(
                    command=NodeDebugSetBreakpointActionRequest(
                        session_id=session_id,
                        thread_id=thread_id,
                        action="set_breakpoint",
                        params=NodeDebugSetBreakpointParams(
                            path=path,
                            line=line,
                            condition=condition,
                            hit_condition=hitCondition,
                            log_message=logMessage,
                        ),
                    ),
                    actor="ai",
                    tool_name="add_logpoint",
                    tool_call_id=tool_call_id,
                ),
            )
        except Exception as error:  # noqa: BLE001 - 参数错误也必须返回可审计的工具结果
            return await self._failure_from_error("add_logpoint", error)

    async def remove_breakpoint(self, fileFullPath: str, line: int) -> str:
        session_id, thread_id = self._owner
        try:
            path = self._node_debug_service.workspace_relative_path(
                fileFullPath, "fileFullPath"
            )
            state = await self._node_debug_service.get_state(session_id, thread_id)
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
                    command=NodeDebugClearBreakpointActionRequest(
                        session_id=session_id,
                        thread_id=thread_id,
                        action="clear_breakpoint",
                        params=NodeDebugClearBreakpointParams(
                            breakpoint_id=breakpoint.breakpoint_id,
                        ),
                    ),
                    actor="ai",
                    tool_name="remove_breakpoint",
                    tool_call_id=tool_call_id,
                ),
            )
        except Exception as error:  # noqa: BLE001 - 参数错误也必须返回可审计的工具结果
            return await self._failure_from_error("remove_breakpoint", error)

    async def clear_all_breakpoints(self) -> str:
        session_id, thread_id = self._owner
        return await self._invoke(
            "clear_all_breakpoints",
            lambda tool_call_id: self._node_debug_service.clear_all_breakpoints(
                session_id,
                thread_id=thread_id,
                actor="ai",
                tool_name="clear_all_breakpoints",
                tool_call_id=tool_call_id,
            ),
        )

    async def list_breakpoints(self) -> str:
        session_id, thread_id = self._owner
        return await self._invoke(
            "list_breakpoints",
            lambda _tool_call_id: self._node_debug_service.get_state(
                session_id,
                thread_id,
            ),
        )

    async def list_variable_names(self, scope: DebugScope | None = None) -> str:
        session_id, thread_id = self._owner
        selected_scope = scope or "all"
        try:
            variables = await self._node_debug_service.get_variables(
                session_id=session_id,
                thread_id=thread_id,
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
        session_id, thread_id = self._owner
        selected_scope = scope or "all"
        try:
            variables = await self._node_debug_service.get_variables(
                session_id=session_id,
                thread_id=thread_id,
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
        session_id, thread_id = self._owner
        return await self._invoke(
            "evaluate_expression",
            lambda tool_call_id: self._node_debug_service.apply_action(
                command=NodeDebugEvaluateActionRequest(
                    session_id=session_id,
                    thread_id=thread_id,
                    action="evaluate",
                    params=NodeDebugEvaluateParams(expression=expression),
                ),
                actor="ai",
                tool_name="evaluate_expression",
                tool_call_id=tool_call_id,
            ),
        )


def create_debugging_tools(
    *,
    session_id: str,
    node_debug_service: NodeDebugService,
    invocation_context: ToolInvocationContext,
    thread_binding: ThreadRuntimeBinding | None = None,
) -> list[BaseTool]:
    return DebuggingToolFactory(
        session_id=session_id,
        node_debug_service=node_debug_service,
        invocation_context=invocation_context,
        thread_binding=thread_binding,
    ).build()


def create_debugging_tool(context: CustomToolFactoryContext) -> BaseTool:
    """创建一个只能通过 invoke_extension_tool 调用的源码调试扩展工具。"""
    node_debug_service = context.node_debug_service
    if node_debug_service is None:
        raise RuntimeError("调试扩展工具需要由 Agent runtime 注入 NodeDebugService")
    raw_tool_name = context.tool_options.get("tool_name")
    if not isinstance(raw_tool_name, str) or not raw_tool_name.strip():
        raise ValueError("调试扩展工具 options.tool_name 必须是非空字符串")
    factory = DebuggingToolFactory(
        session_id=context.session_id,
        node_debug_service=node_debug_service,
        invocation_context=context.invocation_context,
    )
    return factory.build_tool(raw_tool_name)


__all__ = [
    "create_debugging_tool",
    "create_debugging_tools",
]
