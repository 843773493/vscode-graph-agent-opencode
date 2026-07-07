from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Protocol

from langchain_core.tools import BaseTool

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.abstractions.session_resources import (
    BackgroundTaskRegistryProtocol,
    TerminalManagerClientProtocol,
)
from app.abstractions.skill_tool_context import (
    SkillToolConfigProtocol,
    SkillToolMessageProtocol,
    SkillToolSessionProtocol,
)
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_orchestrator import SessionOrchestratorProtocol


SkillToolSpec = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SkillToolFactoryContext:
    session_id: str
    agent_id: str
    sender_agent_id: str
    workspace_root: Path
    background_task_registry: BackgroundTaskRegistryProtocol
    background_message_bus: BackgroundMessageBusProtocol
    job_event_bus: JobEventBusProtocol
    job_service: JobServiceProtocol | None
    message_service: SkillToolMessageProtocol
    session_service: SkillToolSessionProtocol
    session_orchestrator: SessionOrchestratorProtocol
    config_service: SkillToolConfigProtocol
    terminal_manager_client: TerminalManagerClientProtocol


@dataclass(frozen=True, slots=True)
class SkillToolBundle:
    tools: list[BaseTool]
    hidden_tool_names: set[str]


class SkillToolFactory(Protocol):
    def __call__(self, context: SkillToolFactoryContext) -> BaseTool:
        """根据当前 session runtime context 构建 skill-only 工具。"""
        ...


def _load_factory(factory_path: str) -> SkillToolFactory:
    module_name, separator, qualname = factory_path.partition(":")
    if not separator or not module_name or not qualname:
        raise ValueError(
            "skill-only 工具 factory 必须使用 'module.path:factory_name' 格式，"
            f"实际值: {factory_path!r}"
        )

    module = import_module(module_name)
    target: object = module
    for attr_name in qualname.split("."):
        target = getattr(target, attr_name)

    if not callable(target):
        raise TypeError(f"skill-only 工具 factory 不可调用: {factory_path}")
    return target


def _spec_name(spec: object) -> str:
    if isinstance(spec, str):
        raise ValueError(
            "skill_only 不再支持只写工具名。"
            "请配置为 {\"name\": \"tool_name\", \"factory\": \"module.path:create_tool\"}"
        )
    if not isinstance(spec, Mapping):
        raise TypeError(f"skill_only 条目必须是对象，实际类型: {type(spec).__name__}")

    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"skill_only 条目缺少 name: {spec}")
    return name


def _normalize_spec(spec: object) -> tuple[str, SkillToolFactory]:
    name = _spec_name(spec)
    if not isinstance(spec, Mapping):
        raise TypeError(f"skill_only 条目必须是对象，实际类型: {type(spec).__name__}")

    factory_path = spec.get("factory")
    if not isinstance(factory_path, str) or not factory_path.strip():
        raise ValueError(f"skill_only 条目缺少 factory: {spec}")

    return name, _load_factory(factory_path)


def skill_tool_spec_names(specs: Iterable[object]) -> set[str]:
    names: set[str] = set()
    for spec in specs:
        names.add(_spec_name(spec))
    return names


def build_skill_tools(
    specs: Iterable[object],
    *,
    context: SkillToolFactoryContext,
) -> list[BaseTool]:
    """构建可由 skill 的 allowed-tools 暴露的隐藏工具集。"""
    tools: list[BaseTool] = []
    seen_names: set[str] = set()
    for spec in specs:
        name, factory = _normalize_spec(spec)
        if name in seen_names:
            raise ValueError(f"重复的 skill-only 工具: {name}")
        seen_names.add(name)

        tool = factory(context)
        if not isinstance(tool, BaseTool):
            raise TypeError(
                f"skill-only 工具 factory 必须返回 BaseTool: name={name}, "
                f"actual={type(tool).__name__}"
            )
        if tool.name != name:
            raise ValueError(
                f"skill-only 工具声明名与实际工具名不一致: declared={name}, actual={tool.name}"
            )
        tools.append(tool)

    return tools


def build_skill_tool_bundle(
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
    message_service: SkillToolMessageProtocol,
    session_service: SkillToolSessionProtocol,
    session_orchestrator: SessionOrchestratorProtocol,
    config_service: SkillToolConfigProtocol,
    terminal_manager_client: TerminalManagerClientProtocol,
) -> SkillToolBundle:
    context = SkillToolFactoryContext(
        session_id=session_id,
        agent_id=agent_id,
        sender_agent_id=sender_agent_id,
        workspace_root=workspace_root,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        job_service=job_service,
        message_service=message_service,
        session_service=session_service,
        session_orchestrator=session_orchestrator,
        config_service=config_service,
        terminal_manager_client=terminal_manager_client,
    )
    tools = build_skill_tools(specs, context=context)
    return SkillToolBundle(
        tools=tools,
        hidden_tool_names={tool.name for tool in tools},
    )
