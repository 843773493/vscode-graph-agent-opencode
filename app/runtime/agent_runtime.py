from __future__ import annotations

from typing import Any, Protocol, TYPE_CHECKING

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.abstractions.session_subagent import SessionSubagentProtocol
from app.abstractions.team import TeamCoordinationProtocol
from app.agents.agent_factory import create_runtime_deep_agent_for_session, resolve_agent_id
from app.agents.graph_tool_adapter import extract_agent_tools_by_name
from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.policy import catalog_group_for_tool, custom_tool_spec_names
from app.agents.skill_runtime import discover_workspace_custom_tool_skill_map
from app.services.infrastructure.config_service import ConfigService
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from langchain_core.tools import BaseTool
from app.abstractions.session_context import (
    SessionContextQueryProtocol,
    WorkspaceSessionContextClientProtocol,
)

if TYPE_CHECKING:
    from app.services.business.message_service import MessageService
    from app.services.business.session_service import SessionService


class AgentRuntimeDependencyProvider(Protocol):
    def get_message_service(self) -> "MessageService": ...

    def get_session_service(self) -> "SessionService": ...

    def get_session_orchestrator(self) -> SessionOrchestratorProtocol: ...

    def get_session_subagent_service(self) -> SessionSubagentProtocol: ...

    def get_team_service(self) -> TeamCoordinationProtocol: ...

    def get_job_service(self) -> JobServiceProtocol: ...

    def get_checkpointer(self) -> BaseCheckpointSaver: ...

    def get_terminal_manager_client(self) -> TerminalManagerClient: ...

    def get_browser_manager_client(self) -> BrowserManagerClient: ...

    def get_session_context_query_service(self) -> SessionContextQueryProtocol: ...

    def get_workspace_session_context_client(
        self,
    ) -> WorkspaceSessionContextClientProtocol: ...

    def get_mcp_tools(self) -> list[BaseTool]: ...


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
    model_routing_enabled: bool = True,
    tool_denylist: set[str] | None = None,
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
        job_service=dependency_provider.get_job_service(),
        message_service=dependency_provider.get_message_service(),
        session_service=dependency_provider.get_session_service(),
        session_orchestrator=dependency_provider.get_session_orchestrator(),
        session_subagent_service=dependency_provider.get_session_subagent_service(),
        team_service=dependency_provider.get_team_service(),
        terminal_manager_client=dependency_provider.get_terminal_manager_client(),
        browser_manager_client=dependency_provider.get_browser_manager_client(),
        session_context_query_service=(
            dependency_provider.get_session_context_query_service()
        ),
        workspace_session_context_client=(
            dependency_provider.get_workspace_session_context_client()
        ),
        mcp_tools=dependency_provider.get_mcp_tools(),
        checkpointer=checkpointer,
        name=name or resolved_agent_id,
        override_model=override_model,
        model_routing_enabled=model_routing_enabled,
        tool_denylist=tool_denylist,
    )

def get_workspace_custom_tool_skill_sources(
    *,
    agent_id: str,
    config_service: ConfigService,
) -> dict[str, list[str]]:
    """返回当前 workspace 中自定义扩展工具到 skill 名称的映射。"""
    custom_tool_names = get_configured_custom_tool_names(
        agent_id=agent_id,
        config_service=config_service,
    )
    return discover_workspace_custom_tool_skill_map(custom_tool_names=custom_tool_names)


def get_configured_custom_tool_names(
    *,
    agent_id: str,
    config_service: ConfigService,
) -> set[str]:
    """返回当前 agent 策略最终启用的自定义扩展工具名。"""
    tool_config = config_service.get_agent_tool_config(agent_id)
    custom_tool_names = custom_tool_spec_names(
        tool_config.get("custom", []),
        context=f"agent {agent_id} 的 tools.custom",
    )
    policy = config_service.resolve_agent_tool_policy(agent_id)
    return set(custom_tool_names & policy.enabled_names)


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
        # tool_call_schema 是 LangChain 面向模型公开的权威参数模型；args_schema
        # 还包含 ToolRuntime 等运行时注入字段，不能用于工具目录或模型请求。
        parameters = export_model_tool_json_schema(tool)
        metadata = dict(getattr(tool, "metadata", None) or {})
        mcp_server_id = metadata.get("mcp_server_id")
        group = catalog_group_for_tool(tool_name)
        group_fields = (
            {
                "group_id": f"mcp:{mcp_server_id}",
                "group_name": f"MCP · {mcp_server_id}",
                "kind": "mcp",
            }
            if isinstance(mcp_server_id, str) and mcp_server_id
            else group.as_catalog_fields()
        )
        tools.append(
            {
                "id": tool_name,
                "name": tool_name,
                "description": getattr(tool, "description", ""),
                "parameters": parameters,
                "category": group_fields["kind"],
                **group_fields,
            }
        )
    return tools
