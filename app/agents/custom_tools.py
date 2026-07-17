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


CustomToolSpec = Mapping[str, object]


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


def _spec_name(spec: object) -> str:
    if isinstance(spec, str):
        raise ValueError(
            "tools.custom 不支持只写工具名。"
            "请配置为 {\"name\": \"tool_name\", \"factory\": \"module.path:create_tool\"}"
        )
    if not isinstance(spec, Mapping):
        raise TypeError(f"tools.custom 条目必须是对象，实际类型: {type(spec).__name__}")

    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"tools.custom 条目缺少 name: {spec}")
    return name


def _normalize_spec(
    spec: object,
) -> tuple[str, CustomToolFactory, Mapping[str, object]]:
    name = _spec_name(spec)
    if not isinstance(spec, Mapping):
        raise TypeError(f"tools.custom 条目必须是对象，实际类型: {type(spec).__name__}")

    factory_path = spec.get("factory")
    if not isinstance(factory_path, str) or not factory_path.strip():
        raise ValueError(f"tools.custom 条目缺少 factory: {spec}")

    options = spec.get("options", {})
    if not isinstance(options, Mapping):
        raise TypeError(f"tools.custom[{name}].options 必须是对象")

    return name, _load_factory(factory_path), dict(options)


def custom_tool_spec_names(specs: Iterable[object]) -> set[str]:
    names: set[str] = set()
    for spec in specs:
        names.add(_spec_name(spec))
    return names


def build_custom_tools(
    specs: Iterable[object],
    *,
    context: CustomToolFactoryContext,
) -> list[BaseTool]:
    """构建可由固定扩展入口调用的自定义工具集。"""
    tools: list[BaseTool] = []
    seen_names: set[str] = set()
    for spec in specs:
        name, factory, options = _normalize_spec(spec)
        if name in seen_names:
            raise ValueError(f"重复的自定义扩展工具: {name}")
        seen_names.add(name)

        tool = factory(replace(context, tool_options=options))
        if not isinstance(tool, BaseTool):
            raise TypeError(
                f"自定义扩展工具 factory 必须返回 BaseTool: name={name}, "
                f"actual={type(tool).__name__}"
            )
        if tool.name != name:
            raise ValueError(
                f"自定义扩展工具声明名与实际工具名不一致: declared={name}, actual={tool.name}"
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
    )
    tools = build_custom_tools(specs, context=context)
    return CustomToolBundle(tools=tools)
