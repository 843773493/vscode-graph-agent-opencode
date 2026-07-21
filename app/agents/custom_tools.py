from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from importlib import import_module
from pathlib import Path
from typing import Protocol

from langchain_core.tools import BaseTool

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.abstractions.session_resources import (
    BrowserManagerClientProtocol,
    BackgroundTaskRegistryProtocol,
    TerminalManagerClientProtocol,
)
from app.abstractions.custom_tool_context import (
    CustomToolConfigProtocol,
)
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.abstractions.session_context import (
    SessionContextQueryProtocol,
    WorkspaceSessionContextClientProtocol,
)
from app.agents.model_tool_schema import get_model_tool_schema
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.policy import parse_custom_tool_specs


@dataclass(frozen=True, slots=True)
class CustomToolFactoryContext:
    session_id: str
    agent_id: str
    sender_agent_id: str
    workspace_root: Path
    background_task_registry: BackgroundTaskRegistryProtocol
    background_message_bus: BackgroundMessageBusProtocol
    job_event_bus: JobEventBusProtocol
    job_service: JobServiceProtocol | None
    session_context_query_service: SessionContextQueryProtocol
    workspace_session_context_client: WorkspaceSessionContextClientProtocol
    session_orchestrator: SessionOrchestratorProtocol
    config_service: CustomToolConfigProtocol
    terminal_manager_client: TerminalManagerClientProtocol
    browser_manager_client: BrowserManagerClientProtocol
    invocation_context: ToolInvocationContext
    tool_options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CustomToolBundle:
    tools: list[BaseTool]


class CustomToolFactory(Protocol):
    def __call__(self, context: CustomToolFactoryContext) -> BaseTool:
        """根据当前 session runtime context 构建自定义扩展工具。"""
        ...


def _load_factory(factory_path: str) -> CustomToolFactory:
    module_name, separator, qualname = factory_path.partition(":")
    if not separator or not module_name or not qualname:
        raise ValueError(
            "自定义扩展工具 factory 必须使用 'module.path:factory_name' 格式，"
            f"实际值: {factory_path!r}"
        )

    module = import_module(module_name)
    target: object = module
    for attr_name in qualname.split("."):
        target = getattr(target, attr_name)

    if not callable(target):
        raise TypeError(f"自定义扩展工具 factory 不可调用: {factory_path}")
    return target


def build_custom_tools(
    specs: Iterable[object],
    *,
    context: CustomToolFactoryContext,
) -> list[BaseTool]:
    """构建可由固定扩展入口调用的自定义工具集。"""
    tools: list[BaseTool] = []
    for spec in parse_custom_tool_specs(specs):
        factory = _load_factory(spec.factory_path)
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


def build_custom_tool_bundle(
    specs: Iterable[object],
    *,
    session_id: str,
    agent_id: str,
    sender_agent_id: str,
    workspace_root: Path,
    background_task_registry: BackgroundTaskRegistryProtocol,
    background_message_bus: BackgroundMessageBusProtocol,
    job_event_bus: JobEventBusProtocol,
    job_service: JobServiceProtocol | None,
    session_context_query_service: SessionContextQueryProtocol,
    workspace_session_context_client: WorkspaceSessionContextClientProtocol,
    session_orchestrator: SessionOrchestratorProtocol,
    config_service: CustomToolConfigProtocol,
    terminal_manager_client: TerminalManagerClientProtocol,
    browser_manager_client: BrowserManagerClientProtocol,
    invocation_context: ToolInvocationContext,
) -> CustomToolBundle:
    context = CustomToolFactoryContext(
        session_id=session_id,
        agent_id=agent_id,
        sender_agent_id=sender_agent_id,
        workspace_root=workspace_root,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        job_service=job_service,
        session_context_query_service=session_context_query_service,
        workspace_session_context_client=workspace_session_context_client,
        session_orchestrator=session_orchestrator,
        config_service=config_service,
        terminal_manager_client=terminal_manager_client,
        browser_manager_client=browser_manager_client,
        invocation_context=invocation_context,
    )
    tools = build_custom_tools(specs, context=context)
    return CustomToolBundle(tools=tools)
