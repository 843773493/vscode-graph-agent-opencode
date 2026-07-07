from __future__ import annotations

from typing import Any, Protocol, TYPE_CHECKING

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.agents.agent_factory import (
    build_candidate_models_for_agent_content,
    create_runtime_deep_agent_for_session,
    resolve_agent_id,
)
from app.agents.graph_tool_adapter import extract_agent_tools_by_name
from app.agents.skill_runtime import discover_workspace_skill_tool_map
from app.agents.skill_tools import skill_tool_spec_names
from app.services.infrastructure.config_service import ConfigService
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient

if TYPE_CHECKING:
    from app.services.business.message_service import MessageService
    from app.services.business.session_service import SessionService


class AgentRuntimeDependencyProvider(Protocol):
    def get_message_service(self) -> "MessageService": ...

    def get_session_service(self) -> "SessionService": ...

    def get_session_orchestrator(self) -> SessionOrchestratorProtocol: ...

    def get_checkpointer(self) -> BaseCheckpointSaver: ...

    def get_terminal_manager_client(self) -> TerminalManagerClient: ...


def build_session_agent_runtime(
    *,
    session_id: str,
    agent_id: str,
    config_service: ConfigService,
    background_task_registry: BackgroundTaskRegistry,
    background_message_bus: BackgroundMessageBus,
    job_event_bus: JobEventBusProtocol,
    dependency_provider: AgentRuntimeDependencyProvider,
    name: str | None = None,
    override_model: Any = None,
    fallback_middleware_enabled: bool = True,
) -> Any:
    resolved_agent_id = resolve_agent_id(agent_id, config_service)
    checkpointer = dependency_provider.get_checkpointer()
    if checkpointer is None:
        raise RuntimeError("AgentRuntimeDependencyProvider 必须显式提供 checkpointer")
    return create_runtime_deep_agent_for_session(
        session_id=session_id,
        agent_id=resolved_agent_id,
        config_service=config_service,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        message_service=dependency_provider.get_message_service(),
        session_service=dependency_provider.get_session_service(),
        session_orchestrator=dependency_provider.get_session_orchestrator(),
        terminal_manager_client=dependency_provider.get_terminal_manager_client(),
        checkpointer=checkpointer,
        name=name or resolved_agent_id,
        override_model=override_model,
        fallback_middleware_enabled=fallback_middleware_enabled,
    )


def build_candidate_models_for_session_request(
    *,
    agent_id: str,
    config_service: ConfigService,
    content: object,
) -> list[Any]:
    return build_candidate_models_for_agent_content(
        agent_id=agent_id,
        config_service=config_service,
        content=content,
    )


def get_workspace_skill_tool_sources(
    *,
    agent_id: str,
    config_service: ConfigService,
) -> dict[str, list[str]]:
    """返回当前 workspace 中 skill-only 工具到 skill 名称的映射。"""
    tool_config = config_service.get_agent_tool_config(agent_id)
    hidden_tool_names = skill_tool_spec_names(tool_config.get("skill_only", []))
    return discover_workspace_skill_tool_map(hidden_tool_names=hidden_tool_names)


def build_agent_tool_definitions(agent: Any) -> list[dict[str, Any]]:
    """返回 Agent 当前可用工具定义，隐藏 DeepAgents 图结构细节。"""
    tool_map = extract_agent_tools_by_name(agent)
    if not tool_map:
        raise RuntimeError(
            "无法从 Agent 实例中提取工具列表。\n"
            "Agent 图中未找到包含 tools_by_name 的节点。\n"
            "这是严重错误，需要立即修复，不能静默降级。"
        )

    tools: list[dict[str, Any]] = []
    for tool_name, tool in tool_map.items():
        args_schema = getattr(tool, "args_schema", None)
        if hasattr(args_schema, "model_json_schema"):
            parameters = args_schema.model_json_schema()
        elif hasattr(args_schema, "schema"):
            parameters = args_schema.schema()
        else:
            parameters = {"type": "object", "properties": {}}
        tools.append(
            {
                "id": tool_name,
                "name": tool_name,
                "description": getattr(tool, "description", ""),
                "parameters": parameters,
                "category": "general",
            }
        )
    return tools
