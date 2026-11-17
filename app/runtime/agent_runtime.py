from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from app.abstractions.communication import SessionCommunicationProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_context import (
    SessionContextQueryProtocol,
    WorkspaceSessionContextClientProtocol,
)
from app.abstractions.session_messages import SessionMessageOrchestratorProtocol
from app.abstractions.session_subagent import SessionSubagentProtocol
from app.abstractions.team import TeamCoordinationProtocol
from app.abstractions.tool_selection import ToolSelectionReader
from app.core.lifecycle import LifetimeScope
from app.services.business.browser_resources.owner import BrowserResourceOwner
from app.services.business.terminal_resources.owner import TerminalResourceOwner
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.mcp.catalog_owner import McpCatalogOwner
from app.services.infrastructure.mcp.connection_service import McpConnectionService
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from app.services.orchestration.background_work.mailbox import BackgroundMailbox
from app.services.orchestration.background_work.registry import BackgroundTaskRegistry

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.tools import BaseTool

    from app.agents.graph_binding import GraphBindingStorePort
    from app.agents.instruction_producers import InstructionProducerSpec
    from app.agents.model_capability_routing import CapabilityRoutingMiddleware
    from app.agents.skill_catalog import PublishedSkillCatalog
    from app.agents.tool_identity import ToolDefinitionLike
    from app.services.business.message_service import MessageService
    from app.services.business.session_goal_service import SessionGoalService
    from app.services.business.session_service import SessionService
    from app.services.infrastructure.rollout_context.checkpoint.saver import (
        RolloutCheckpointSaver,
    )
    from app.services.orchestration.event_stream.contracts import AgentEventSource
    from app.services.orchestration.resource_activation import (
        ResourceActivationCoordinator,
    )


AgentAssemblyMode = Literal["execution", "tool_catalog"]
AGENT_GRAPH_RECURSION_LIMIT = 9999


@dataclass(frozen=True, slots=True)
class AgentModelSetup:
    """模型与其配置指令的准备结果；不持有 Thread、工具或执行图。"""

    model: BaseChatModel
    routing_middleware: CapabilityRoutingMiddleware
    instruction_producers: tuple[InstructionProducerSpec, ...]


def prepare_agent_model(
    agent_id: str,
    config_service: ConfigService | None = None,
    *,
    prompt_cache_key: str | None = None,
    preferred_provider_id: str | None = None,
) -> AgentModelSetup:
    """准备配置中的模型候选、路由和指令来源，供执行与压缩预览共用。"""
    from app.agents.instruction_producers import runtime_instruction_producers
    from app.agents.model_capability_routing import (
        CapabilityRoutingMiddleware,
        build_provider_model_candidate,
    )
    from app.runtime.provider_model import (
        build_model_from_provider,
        provider_build_failure,
    )

    if config_service is None:
        raise RuntimeError("prepare_agent_model 需要显式传入 ConfigService")
    runtime_config = config_service.get_agent_runtime_config(
        agent_id,
        preferred_provider_id=preferred_provider_id,
    )
    providers = runtime_config["providers"]
    candidates = []
    for index, provider in enumerate(providers):
        try:
            model = build_model_from_provider(
                provider,
                runtime_config,
                prompt_cache_key=prompt_cache_key,
            )
        except Exception as error:
            provider_id = str(provider.get("id") or provider.get("model") or "<unknown>")
            detail = provider_build_failure(provider, error)
            if index == 0:
                raise RuntimeError(
                    f"当前选择的模型配置不可用: provider_id={provider_id}; {detail}"
                ) from error
            continue
        candidates.append(
            build_provider_model_candidate(provider=provider, model=model)
        )
    if not candidates:
        raise RuntimeError("未能构建任何模型实例")
    return AgentModelSetup(
        model=candidates[0].model,
        routing_middleware=CapabilityRoutingMiddleware(candidates),
        instruction_producers=runtime_instruction_producers(
            agent_id=agent_id, content=runtime_config["system_prompt"],
            config_revision=config_service.get_revision(),
            runtime_identity_description=runtime_config["runtime_identity_description"],
            provider_candidates=[
                (candidate.provider_id, candidate.model_id)
                for candidate in candidates
            ],
        ),
    )


class AgentRuntimeDependencyProvider(Protocol):
    def get_goal_service(self) -> SessionGoalService: ...
    def get_message_service(self) -> MessageService: ...

    def get_session_service(self) -> SessionService: ...

    def get_session_orchestrator(self) -> SessionMessageOrchestratorProtocol: ...

    def get_session_subagent_service(self) -> SessionSubagentProtocol: ...

    def get_team_service(self) -> TeamCoordinationProtocol: ...

    def get_job_service(self) -> JobServiceProtocol: ...

    def get_checkpointer(self) -> RolloutCheckpointSaver: ...

    def get_resource_navigation(self) -> ResourceNavigation: ...

    def get_text_mutations(self) -> WorkspaceTextMutationOwner: ...

    def get_terminal_resource_owner(self) -> TerminalResourceOwner: ...

    def get_browser_resource_owner(self) -> BrowserResourceOwner: ...

    def get_session_context_query_service(self) -> SessionContextQueryProtocol: ...

    def get_workspace_session_context_client(
        self,
    ) -> WorkspaceSessionContextClientProtocol: ...

    def get_session_communication_service(
        self,
    ) -> SessionCommunicationProtocol: ...

    def get_mcp_tools(self) -> list[BaseTool]: ...

    def get_mcp_catalog_owner(self) -> McpCatalogOwner: ...

    def get_mcp_connection_service(self) -> McpConnectionService: ...

    def get_node_debug_service(self) -> NodeDebugService: ...


def load_agent_assembly_factory() -> Callable[..., AgentEventSource | list[ToolDefinitionLike]]:
    """加载内置执行依赖，不构建模型、Thread 或配置中的自定义工具。"""
    from importlib import import_module

    from app.agents.agent_factory import assemble_configured_agent

    # SDK 首次访问 chat/responses 会同步加载整套资源模块，统一在就绪前的 worker 中完成。
    import_module("openai.resources")
    return assemble_configured_agent


def assemble_agent(
    *,
    session_id: str,
    thread_id: str,
    agent_id: str,
    tool_selection_reader: ToolSelectionReader,
    config_service: ConfigService,
    background_task_registry: BackgroundTaskRegistry,
    background_mailbox: BackgroundMailbox,
    job_event_bus: JobEventBusProtocol,
    dependency_provider: AgentRuntimeDependencyProvider,
    mcp_tools: Sequence[BaseTool],
    mcp_catalog_owner: McpCatalogOwner | None = None,
    resource_registry: ResourceRegistry,
    resource_activation: ResourceActivationCoordinator | None,
    name: str | None = None,
    override_model: Any = None,
    model_routing_enabled: bool = True,
    tool_denylist: set[str] | None = None,
    execution_overrides: Mapping[str, bool] | None = None,
    model_visibility_overrides: Mapping[str, bool] | None = None,
    preferred_provider_id: str | None = None,
    tool_timeout_seconds: float | None = None,
    workspace_file_resource_registry: WorkspaceFileResourceRegistry | None = None,
    reactor_lifetime_scope: LifetimeScope | None = None,
    skill_catalog: PublishedSkillCatalog | None = None,
    graph_binding_store: GraphBindingStorePort,
    workspace_root: Path,
    include_team_tools: bool = False,
    assembly_mode: AgentAssemblyMode = "execution",
) -> AgentEventSource | list[ToolDefinitionLike]:
    """按明确模式装配：execution 返回图，tool_catalog 返回工具注册列表。"""
    assemble_configured_agent = load_agent_assembly_factory()
    resolved_agent_id = config_service.resolve_agent_id(agent_id)
    checkpointer = dependency_provider.get_checkpointer()
    if checkpointer is None:
        raise RuntimeError("AgentRuntimeDependencyProvider 必须显式提供 checkpointer")
    return assemble_configured_agent(
        session_id=session_id,
        thread_id=thread_id,
        agent_id=resolved_agent_id,
        tool_selection_reader=tool_selection_reader,
        config_service=config_service,
        background_task_registry=background_task_registry,
        background_mailbox=background_mailbox,
        job_event_bus=job_event_bus,
        job_service=dependency_provider.get_job_service(),
        goal_service=dependency_provider.get_goal_service(),
        message_service=dependency_provider.get_message_service(),
        session_service=dependency_provider.get_session_service(),
        session_orchestrator=dependency_provider.get_session_orchestrator(),
        session_subagent_service=dependency_provider.get_session_subagent_service(),
        team_service=dependency_provider.get_team_service(),
        terminal_resource_owner=dependency_provider.get_terminal_resource_owner(),
        browser_resource_owner=dependency_provider.get_browser_resource_owner(),
        session_context_query_service=(
            dependency_provider.get_session_context_query_service()
        ),
        workspace_session_context_client=(
            dependency_provider.get_workspace_session_context_client()
        ),
        session_communication_service=(
            dependency_provider.get_session_communication_service()
        ),
        mcp_tools=mcp_tools,
        mcp_catalog_owner=mcp_catalog_owner,
        mcp_connection_service=dependency_provider.get_mcp_connection_service(),
        resource_registry=resource_registry,
        resource_activation=resource_activation,
        node_debug_service=dependency_provider.get_node_debug_service(),
        checkpointer=checkpointer,
        resource_navigation=dependency_provider.get_resource_navigation(),
        text_mutations=dependency_provider.get_text_mutations(),
        name=name or resolved_agent_id,
        override_model=override_model,
        model_routing_enabled=model_routing_enabled,
        tool_denylist=tool_denylist,
        execution_overrides=execution_overrides,
        model_visibility_overrides=model_visibility_overrides,
        preferred_provider_id=preferred_provider_id,
        tool_timeout_seconds=tool_timeout_seconds,
        workspace_file_resource_registry=workspace_file_resource_registry,
        reactor_lifetime_scope=reactor_lifetime_scope,
        skill_catalog=skill_catalog,
        graph_binding_store=graph_binding_store,
        include_team_tools=include_team_tools,
        assembly_mode=assembly_mode,
        workspace_root=workspace_root,
    )
