from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from langchain_core.tools import BaseTool

from app.abstractions.background_mailbox import BackgroundMailboxProtocol
from app.abstractions.background_task_registry import BackgroundTaskRegistryProtocol
from app.abstractions.communication import SessionCommunicationProtocol
from app.abstractions.custom_tool_context import (
    CustomToolConfigProtocol,
)
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_context import (
    SessionContextQueryProtocol,
    WorkspaceSessionContextClientProtocol,
)
from app.abstractions.session_messages import SessionMessageOrchestratorProtocol
from app.agents.model_tool_schema import (
    export_model_tool_json_schema,
    get_model_tool_schema,
)
from app.agents.policy import ParsedCustomToolSpec
from app.agents.tool_invocation_context import ToolInvocationContext

if TYPE_CHECKING:
    from app.services.business.browser_resources.owner import BrowserResourceOwner
    from app.services.business.terminal_resources.owner import TerminalResourceOwner
    from app.services.infrastructure.mcp.connection_service import McpConnectionService
    from app.services.infrastructure.node_debug.service import NodeDebugService


@dataclass(frozen=True, slots=True)
class CustomToolFactoryContext:
    agent_id: str
    sender_agent_id: str
    workspace_root: Path
    background_task_registry: BackgroundTaskRegistryProtocol
    # 扩展 factory 的公开字段名；实际依赖是持久邮箱，不是 Job 事件总线。
    background_message_bus: BackgroundMailboxProtocol
    job_event_bus: JobEventBusProtocol
    job_service: JobServiceProtocol | None
    session_context_query_service: SessionContextQueryProtocol
    workspace_session_context_client: WorkspaceSessionContextClientProtocol
    session_orchestrator: SessionMessageOrchestratorProtocol
    config_service: CustomToolConfigProtocol
    terminal_resource_owner: TerminalResourceOwner
    browser_resource_owner: BrowserResourceOwner
    invocation_context: ToolInvocationContext
    session_communication_service: SessionCommunicationProtocol | None = None
    tool_options: Mapping[str, object] = field(default_factory=dict)
    node_debug_service: NodeDebugService | None = None
    mcp_connection_service: McpConnectionService | None = None


class CustomToolFactory(Protocol):
    def __call__(self, context: CustomToolFactoryContext) -> BaseTool:
        """根据当前 session runtime context 构建自定义扩展工具。"""
        ...


def load_custom_tool_factory(factory_path: str) -> CustomToolFactory:
    module_name, separator, qualname = factory_path.partition(":")
    if not separator or not module_name or not qualname:
        raise ValueError(
            "自定义扩展工具 factory 必须使用 'module.path:factory_name' 格式，"
            f"实际值: {factory_path!r}"
        )

    try:
        module = import_module(module_name)
    except (ImportError, ModuleNotFoundError) as exc:
        raise ImportError(
            f"无法导入扩展工具 factory 模块: factory={factory_path!r}"
        ) from exc
    target: object = module
    for attr_name in qualname.split("."):
        if not hasattr(target, attr_name):
            raise AttributeError(
                "扩展工具 factory 属性不存在: "
                f"factory={factory_path!r}, missing={attr_name!r}"
            )
        target = getattr(target, attr_name)

    if not callable(target):
        raise TypeError(f"自定义扩展工具 factory 不可调用: {factory_path}")
    return target


def build_custom_tools(
    specs: Iterable[ParsedCustomToolSpec],
    *,
    context: CustomToolFactoryContext,
) -> list[BaseTool]:
    """消费已校验的声明构建扩展工具，检查 factory 结果与公开 schema。"""
    tools: list[BaseTool] = []
    for spec in specs:
        factory = load_custom_tool_factory(spec.factory_path)
        tool = factory(replace(context, tool_options=spec.options))
        if not isinstance(tool, BaseTool):
            raise TypeError(
                f"自定义扩展工具 factory 必须返回 BaseTool: name={spec.name}, "
                f"actual={type(tool).__name__}"
            )
        if tool.name != spec.name:
            raise ValueError(
                "自定义扩展工具声明名与实际工具名不一致: "
                f"declared={spec.name}, actual={tool.name}"
            )
        export_model_tool_json_schema(tool)
        if not isinstance(tool.tool_call_schema, dict):
            internal_schema = tool.get_input_schema()
            public_schema = get_model_tool_schema(tool)
            internal_fields = set(internal_schema.model_fields)
            public_fields = set(public_schema.model_fields)
            hidden_fields = internal_fields - public_fields
            if hidden_fields:
                raise TypeError(
                    "扩展工具不得通过 ToolRuntime/InjectedToolArg 声明隐藏注入参数；"
                    "扩展工具的后端依赖必须由 CustomToolFactoryContext 容器注入并由闭包读取。"
                    f" tool_name={spec.name} hidden_fields={sorted(hidden_fields)}"
                )
        tools.append(tool)

    return tools
