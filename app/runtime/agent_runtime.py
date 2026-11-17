from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

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


def preload_agent_dependencies() -> None:
    """预加载固定执行依赖，不构建模型、Thread 或配置中的自定义工具。"""
    from importlib import import_module

    import_module("app.agents.agent_factory")
    # SDK 首次访问会同步加载资源模块，统一在就绪前的 worker 中完成。
    import_module("openai.resources")


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
    override_model: BaseChatModel | None = None,
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
    """统一读取配置、注入依赖并选择冻结工厂；按模式返回执行图或工具目录。"""
    from app.agents.graph_binding import (
        DEEP_AGENT_GRAPH_BINDING,
        GRAPH_FACTORY_REGISTRY,
        GraphBindingOwnerKey,
        resolve_graph_binding,
    )
    from app.agents.policy import parse_custom_tool_specs

    preload_agent_dependencies()
    resolved_agent_id = config_service.resolve_agent_id(agent_id)
    checkpointer = dependency_provider.get_checkpointer()
    if checkpointer is None:
        raise RuntimeError("AgentRuntimeDependencyProvider 必须显式提供 checkpointer")
    job_service = dependency_provider.get_job_service()
    goal_service = dependency_provider.get_goal_service()
    message_service = dependency_provider.get_message_service()
    session_service = dependency_provider.get_session_service()
    session_orchestrator = dependency_provider.get_session_orchestrator()
    session_subagent_service = dependency_provider.get_session_subagent_service()
    team_service = dependency_provider.get_team_service()
    terminal_resource_owner = dependency_provider.get_terminal_resource_owner()
    browser_resource_owner = dependency_provider.get_browser_resource_owner()
    session_context_query_service = dependency_provider.get_session_context_query_service()
    workspace_session_context_client = dependency_provider.get_workspace_session_context_client()
    session_communication_service = dependency_provider.get_session_communication_service()
    mcp_connection_service = dependency_provider.get_mcp_connection_service()
    node_debug_service = dependency_provider.get_node_debug_service()
    resource_navigation = dependency_provider.get_resource_navigation()
    text_mutations = dependency_provider.get_text_mutations()
    model_setup = (
        prepare_agent_model(
            agent_id=resolved_agent_id,
            config_service=config_service,
            prompt_cache_key=session_id,
            preferred_provider_id=preferred_provider_id,
        )
        if assembly_mode == "execution" else None
    )
    tool_config = config_service.get_agent_tool_config(resolved_agent_id)
    tool_policy = config_service.resolve_agent_tool_policy(resolved_agent_id)
    confirmation_tool_names = (
        config_service.resolve_agent_confirmation_tool_names(resolved_agent_id)
        & tool_policy.enabled_names
    )
    custom_tool_specs = parse_custom_tool_specs(
        list(tool_config.get("custom", [])),
        context=f"agent {resolved_agent_id} 的 tools.custom",
    )
    configured_custom_tool_names = {spec.name for spec in custom_tool_specs}
    custom_tool_confirmation_names = confirmation_tool_names & configured_custom_tool_names
    direct_confirmation_tool_names = confirmation_tool_names - configured_custom_tool_names
    if session_service is None:
        raise RuntimeError("assemble_agent 需要显式传入 SessionService")
    model = override_model if override_model is not None else (model_setup.model if model_setup is not None else None)
    # 执行只用 Thread 创建时冻结的 selector；工具目录没有持久 Thread owner。
    binding = (
        resolve_graph_binding(
            store=graph_binding_store,
            owner=GraphBindingOwnerKey(session_id, thread_id),
        )
        if assembly_mode == "execution" else DEEP_AGENT_GRAPH_BINDING
    )
    builder = cast(
        "Callable[..., AgentEventSource | list[ToolDefinitionLike]]",
        GRAPH_FACTORY_REGISTRY.resolve(binding),
    )
    return builder(
        model=model,
        response_format=None,
        instruction_producers=model_setup.instruction_producers if model_setup is not None else (),
        tool_selection_reader=tool_selection_reader,
        checkpointer=checkpointer,
        session_id=session_id,
        thread_id=thread_id,
        agent_id=resolved_agent_id,
        model_routing_middleware=model_setup.routing_middleware
        if model_setup is not None and model_routing_enabled and override_model is None
        else None,
        sender_agent_id=None,
        enabled_tool_names=None,
        enabled_runtime_middleware_names=None,
        tool_denylist=set(tool_denylist or set()),
        execution_overrides=execution_overrides,
        model_visibility_overrides=model_visibility_overrides,
        custom_tool_specs=custom_tool_specs,
        name=name or resolved_agent_id,
        background_task_registry=background_task_registry,
        background_mailbox=background_mailbox,
        job_event_bus=job_event_bus,
        job_service=job_service,
        goal_service=goal_service,
        message_service=message_service,
        session_service=session_service,
        session_orchestrator=session_orchestrator,
        session_subagent_service=session_subagent_service,
        team_service=team_service,
        terminal_resource_owner=terminal_resource_owner,
        browser_resource_owner=browser_resource_owner,
        node_debug_service=node_debug_service,
        session_context_query_service=session_context_query_service,
        workspace_session_context_client=workspace_session_context_client,
        session_communication_service=session_communication_service,
        mcp_tools=mcp_tools,
        mcp_catalog_owner=mcp_catalog_owner,
        mcp_connection_service=mcp_connection_service,
        tool_timeout_seconds=tool_timeout_seconds,
        workspace_file_resource_registry=workspace_file_resource_registry,
        reactor_lifetime_scope=reactor_lifetime_scope,
        skill_catalog=skill_catalog,
        resource_registry=resource_registry,
        resource_activation=resource_activation,
        include_team_tools=include_team_tools,
        resource_navigation=resource_navigation,
        text_mutations=text_mutations,
        interrupt_on={tool_name: True for tool_name in direct_confirmation_tool_names},
        custom_tool_confirmation_names=custom_tool_confirmation_names,
        config_service=config_service,
        workspace_root=workspace_root,
        assembly_mode=assembly_mode,
    )
