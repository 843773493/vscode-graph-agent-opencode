from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Protocol

from app.agents.policy import parse_custom_tool_specs
from app.services.infrastructure.config import ConfigSnapshot
from app.services.infrastructure.config_service import ConfigService

if TYPE_CHECKING:
    from app.abstractions.job_event_bus import JobEventBusProtocol
    from app.runtime.agent_runtime import AgentRuntimeDependencyProvider
    from app.services.infrastructure.mcp.catalog_owner import McpCatalogPublication
    from app.services.orchestration.background_work.mailbox import BackgroundMailbox
    from app.services.orchestration.background_work.registry import (
        BackgroundTaskRegistry,
    )


class ToolCatalogReader(Protocol):
    """定义及其配置配对读取；策略投影不得中途混入新配置。"""

    def list_tool_definitions(
        self, agent_id: str | None = None,
    ) -> tuple[ConfigSnapshot, list[dict[str, Any]]]: ...


class AgentToolCatalog:
    """当前管理定义的唯一读取 owner，不取得执行图或合成 SessionThread。"""

    def __init__(
        self, *, config_service: ConfigService,
        dependency_provider: AgentRuntimeDependencyProvider,
        background_task_registry: BackgroundTaskRegistry,
        background_mailbox: BackgroundMailbox,
        job_event_bus: JobEventBusProtocol,
        workspace_root: Path,
    ) -> None:
        self._config_service = config_service
        self._dependencies = dependency_provider
        self._background_tasks = background_task_registry
        self._background_mailbox = background_mailbox
        self._job_event_bus = job_event_bus
        self._workspace_root = workspace_root
        # 构造锁只保护本 owner 的缓存，不占配置／MCP publication 锁。
        self._lock = RLock()
        self._cache: dict[str, tuple[tuple[str, str, int], list[dict[str, Any]]]] = {}

    def list_tool_definitions(
        self, agent_id: str | None = None,
    ) -> tuple[ConfigSnapshot, list[dict[str, Any]]]:
        owner = self._dependencies.get_mcp_catalog_owner()
        with self._lock:
            with self._config_service.publication_lock:
                snapshot = self._config_service.get_active_snapshot()
                publication = owner.publication_snapshot()
            with self._config_service.use_snapshot(snapshot):
                resolved = self._config_service.validate_agent_id(agent_id)
                binding = publication.binding_ref
                key = (snapshot.revision, binding.catalog_revision, binding.generation)
                cached = self._cache.get(resolved)
                if cached is None or cached[0] != key:
                    definitions = self._build_definitions(resolved, publication)
                    # 每个当前 Agent 一份可序列化定义；不保留旧代、闭包或凭据。
                    active_agents = set(self._config_service.list_agents())
                    self._cache = {name: value for name, value in self._cache.items() if name in active_agents}
                    self._cache[resolved] = (key, definitions)
                else:
                    definitions = cached[1]
                result = deepcopy(definitions)
                for definition in result:
                    target = publication.definitions.get(definition["id"])
                    if target is not None:
                        definition["availability"] = target.readiness
                return snapshot, result

    def _build_definitions(
        self, agent_id: str, publication: McpCatalogPublication,
    ) -> list[dict[str, Any]]:
        from app.agents.custom_tools import CustomToolFactoryContext, build_custom_tools
        from app.agents.default_tools import build_default_tools
        from app.agents.middleware_assembly import build_deep_agent_tool_middleware
        from app.agents.summarization.tool_middleware import (
            COMPACT_CONVERSATION_TOOL,
            READ_COMPACTION_HISTORY_TOOL,
        )
        from app.agents.tool_invocation_context import ToolInvocationContext
        from app.agents.tools.skill_loading import SKILL_LOAD_TOOL
        from app.agents.tools.tool_discovery import SEARCH_TOOLS_TOOL
        from app.agents.workspace_backend import build_workspace_backend
        from app.services.mapping.tool_catalog import tools_to_catalog_definitions

        dependencies = self._dependencies
        # 无 Thread 的上下文仅用于构造定义，工具实例不会返回或留在缓存中。
        context = CustomToolFactoryContext(
            agent_id=agent_id, sender_agent_id=agent_id,
            workspace_root=self._workspace_root,
            background_task_registry=self._background_tasks,
            background_message_bus=self._background_mailbox,
            job_event_bus=self._job_event_bus,
            job_service=dependencies.get_job_service(),
            session_context_query_service=dependencies.get_session_context_query_service(),
            workspace_session_context_client=dependencies.get_workspace_session_context_client(),
            session_orchestrator=dependencies.get_session_orchestrator(),
            config_service=self._config_service,
            terminal_resource_owner=dependencies.get_terminal_resource_owner(),
            browser_resource_owner=dependencies.get_browser_resource_owner(),
            invocation_context=ToolInvocationContext(),
            session_communication_service=dependencies.get_session_communication_service(),
            node_debug_service=dependencies.get_node_debug_service(),
            mcp_connection_service=dependencies.get_mcp_connection_service(),
        )
        specs = parse_custom_tool_specs(
            self._config_service.get_agent_tool_config(agent_id).get("custom", []),
            context=f"agent {agent_id} 的 tools.custom",
        )
        custom = build_custom_tools(specs, context=context)
        default = build_default_tools(
            session_id=None, agent_id=agent_id,
            text_mutations=dependencies.get_text_mutations(),
            background_task_registry=self._background_tasks,
            background_mailbox=self._background_mailbox,
            session_subagent_service=dependencies.get_session_subagent_service(),
            team_service=dependencies.get_team_service(),
            terminal_resource_owner=context.terminal_resource_owner,
            invocation_context=context.invocation_context,
            goal_service=dependencies.get_goal_service(),
            user_question_service=dependencies.get_user_question_service(),
            session_communication_service=context.session_communication_service,
            include_test_tools=self._config_service.development_test_tools_enabled(),
            include_team_tools=self._config_service.get_agent_run_mode() == "team",
        )
        middleware = build_deep_agent_tool_middleware(
            backend=build_workspace_backend(self._workspace_root),
            workspace_root=self._workspace_root,
            resource_navigation=dependencies.get_resource_navigation(),
            text_mutations=dependencies.get_text_mutations(),
            artifact_reader=None, permissions=None,
        )
        return tools_to_catalog_definitions(
            [*default, *(tool for item in middleware for tool in item.tools),
             COMPACT_CONVERSATION_TOOL, READ_COMPACTION_HISTORY_TOOL, SKILL_LOAD_TOOL, SEARCH_TOOLS_TOOL,
             *custom],
            extension_definitions=tuple(publication.definitions.values()), custom_specs=specs,
        )
