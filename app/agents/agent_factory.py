from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from boxteam_plugin_sdk.lifecycle import LifetimeScope
from deepagents.middleware.permissions import FilesystemPermission
from langchain.agents.middleware import InterruptOnConfig
from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.structured_output import ResponseFormat
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.abstractions.communication import SessionCommunicationProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.resource_file_content import ResourceFileContent
from app.abstractions.session_context import (
    SessionContextQueryProtocol,
    WorkspaceSessionContextClientProtocol,
)
from app.abstractions.session_subagent import SessionSubagentProtocol
from app.abstractions.team import TeamCoordinationProtocol
from app.abstractions.tool_selection import ToolSelectionReader
from app.agents.custom_tools import CustomToolFactoryContext, build_custom_tools
from app.agents.default_tools import build_default_tools
from app.agents.instruction_producers import (
    InstructionProducerSpec,
    InstructionUpdateBinding,
    register_instruction_producer,
)
from app.agents.llm_logging_middleware import LLMLoggingMiddleware
from app.agents.middleware_assembly import (
    build_deep_agent_middleware,
    build_deep_agent_tool_middleware,
)
from app.agents.middleware_prompts import TEAM_COORDINATION_SYSTEM_PROMPT
from app.agents.model_call_execution import ModelCallExecution
from app.agents.model_call_graph import create_model_call_agent
from app.agents.model_capability_routing import (
    CapabilityRoutingMiddleware,
)
from app.agents.policy import (
    ParsedCustomToolSpec,
    ToolCapabilityPolicy,
    ToolMetadata,
    validate_tool_dependencies,
)
from app.agents.policy.tool_capability import (
    metadata_for_custom_tool,
    metadata_for_tool,
)
from app.agents.skill_catalog import (
    PublishedSkillCatalog,
    build_workspace_skill_catalog,
)
from app.agents.summarization.preflight import NoDurableOwnerCompactionPreflight
from app.agents.summarization.tool_middleware import (
    COMPACT_CONVERSATION_TOOL,
    READ_COMPACTION_HISTORY_TOOL,
)
from app.agents.tool_identity import (
    TOOL_DISCOVERY_NAME,
    ToolDefinitionLike,
    tool_definition_name,
)
from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.agents.tool_output_middleware import ToolOutputMiddleware
from app.agents.tool_set_producer import RegisteredToolSetProducer
from app.agents.tools.extension_invocation import (
    create_extension_catalog_resolver_from_tools,
    create_extension_tool_invoker_tool,
)
from app.agents.tools.skill_loading import create_skill_load_tool
from app.agents.tools.tool_discovery import create_search_tools_tool
from app.agents.workspace_backend import build_workspace_backend
from app.core.failures import (
    mark_public_failure,
)
from app.core.phase_observer import observe_phase_sync
from app.core.resource_uri import parse_session_artifact_uri
from app.core.turn_execution_scope import get_current_turn_execution_scope
from app.domain.itemized.hashing import sha256_jcs
from app.runtime.agent_runtime import (
    AGENT_GRAPH_RECURSION_LIMIT,
    prepare_agent_model,
)
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.business.browser_resources.owner import BrowserResourceOwner
from app.services.business.terminal_resources.owner import TerminalResourceOwner
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.events.channel_events import (
    ContextSourceEvent,
    ContextSourceEventPublisher,
)
from app.services.infrastructure.mcp.catalog_owner import McpCatalogOwner
from app.services.infrastructure.mcp.catalog_publication import (
    McpCatalogResourcePublisher,
)
from app.services.infrastructure.mcp.connection_service import McpConnectionService
from app.services.infrastructure.mcp.extension_catalog import (
    MCP_TOOL_CATALOG_RESOURCE_ID,
    ExtensionCatalogUnavailableError,
    ExtensionTargetBinding,
    ExtensionTargetResolutionError,
)
from app.services.infrastructure.mcp.frozen_catalog import (
    FrozenToolCatalog,
    restore_frozen_extension_dispatch,
)
from app.services.infrastructure.mcp.invocation import (
    ExtensionExecutionTarget,
    McpResourceUse,
)
from app.services.infrastructure.mcp.tool_definitions import (
    SkillToolSource,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.resource_platform.registry.context_source_reactor import (
    ContextSourceReactor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.compaction_boundary_adapter import (
    CompactionPreflightPort,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlStatePort,
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    SkillLoadMode,
)
from app.services.infrastructure.tool_output_store import ToolOutputStore
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from app.services.orchestration.background_work.mailbox import BackgroundMailbox
from app.services.orchestration.background_work.registry import BackgroundTaskRegistry
from app.services.orchestration.resource_activation import ResourceActivationCoordinator

if TYPE_CHECKING:
    from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
    from app.services.business.communication.user_questions import UserQuestionService
    from app.services.business.message_service import MessageService
    from app.services.business.session_goal_service import SessionGoalService
    from app.services.business.session_service import SessionService
    from app.services.orchestration.event_stream.contracts import AgentEventSource


def assemble_deep_agent(
    *,
    model: BaseChatModel | None,
    instruction_producers: Sequence[InstructionProducerSpec],
    tool_selection_reader: ToolSelectionReader,
    checkpointer: BaseCheckpointSaver | None = None,
    session_id: str,
    thread_id: str,
    agent_id: str,
    model_routing_middleware: CapabilityRoutingMiddleware | None = None,
    sender_agent_id: str | None = None,
    enabled_tool_names: set[str] | None = None,
    enabled_runtime_middleware_names: set[str] | None = None,
    tool_denylist: set[str] | None = None,
    custom_tool_specs: Sequence[ParsedCustomToolSpec] = (),
    tools: Sequence[ToolDefinitionLike] | None = None,
    middleware: Sequence[AgentMiddleware] | None = None,
    response_format: ResponseFormat[Any] | type | dict[str, Any] | None = None,
    skill_catalog: PublishedSkillCatalog | None = None,
    permissions: list[FilesystemPermission] | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    custom_tool_confirmation_names: frozenset[str] = frozenset(),
    execution_overrides: Mapping[str, bool] | None = None,
    model_visibility_overrides: Mapping[str, bool] | None = None,
    debug: bool = False,
    name: str | None = None,
    background_task_registry: BackgroundTaskRegistry | None = None,
    background_mailbox: BackgroundMailbox | None = None,
    job_event_bus: JobEventBusProtocol | None = None,
    job_service: JobServiceProtocol | None = None,
    goal_service: SessionGoalService | None = None,
    user_question_service: UserQuestionService | None = None,
    message_service: MessageService | None = None,
    session_service: SessionService | None = None,
    session_orchestrator: object | None = None,
    session_subagent_service: SessionSubagentProtocol | None = None,
    team_service: TeamCoordinationProtocol | None = None,
    config_service: ConfigService | None = None,
    terminal_resource_owner: TerminalResourceOwner | None = None,
    browser_resource_owner: BrowserResourceOwner | None = None,
    node_debug_service: NodeDebugService | None = None,
    session_context_query_service: SessionContextQueryProtocol | None = None,
    workspace_session_context_client: WorkspaceSessionContextClientProtocol
    | None = None,
    session_communication_service: SessionCommunicationProtocol | None = None,
    mcp_tools: Sequence[BaseTool] | None = None,
    mcp_catalog_owner: McpCatalogOwner | None = None,
    mcp_connection_service: McpConnectionService | None = None,
    tool_timeout_seconds: float | None = None,
    workspace_file_resource_registry: WorkspaceFileResourceRegistry | None = None,
    reactor_lifetime_scope: LifetimeScope | None = None,
    resource_registry: ResourceRegistry,
    resource_activation: ResourceActivationCoordinator | None,
    workspace_root: Path,
    resource_navigation: ResourceNavigation,
    text_mutations: WorkspaceTextMutationOwner,
    include_team_tools: bool = False,
    preferred_model: ModelReferenceDTO | None = None,
    model_routing_enabled: bool = True,
) -> AgentEventSource:
    """装配真实 Thread 的单代图，本地工具检查先于模型 SDK 构造。"""
    if checkpointer is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 checkpointer")
    if config_service is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 ConfigService")
    if session_service is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 SessionService")

    resolved_sender_agent_id = sender_agent_id or agent_id
    resolved_tool_denylist = set(tool_denylist or set())
    resolved_execution_overrides = dict(execution_overrides or {})
    resolved_model_visibility_overrides = dict(model_visibility_overrides or {})

    capability_resolver = config_service.get_tool_policy_resolver(agent_id)
    thread_binding = ThreadRuntimeBinding(session_id=session_id, thread_id=thread_id)
    tool_invocation_context = ToolInvocationContext(
        tool_timeout_seconds=tool_timeout_seconds,
        thread_binding=thread_binding,
    )

    if background_task_registry is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 BackgroundTaskRegistry")
    if background_mailbox is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 BackgroundMailbox")
    if job_event_bus is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 JobEventBus")
    if message_service is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 MessageService")
    if session_orchestrator is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 SessionMessageOrchestrator")
    if session_subagent_service is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 SessionSubagentService")
    if include_team_tools and team_service is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 TeamCoordinationService")
    if job_service is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 JobService")
    if session_context_query_service is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 SessionContextQueryService")
    if workspace_session_context_client is None:
        raise RuntimeError("assemble_deep_agent 需要显式传入 WorkspaceSessionContextClient")

    workspace_root = workspace_root.resolve()

    resolved_skills = (
        build_workspace_skill_catalog(
            workspace_root, registry=resource_registry,
            file_registry=workspace_file_resource_registry,
        )
        if skill_catalog is None and workspace_file_resource_registry is not None
        else skill_catalog
    )
    if resolved_skills is None:
        raise RuntimeError("构建 Workspace Skill catalog 需要 WorkspaceFileResourceRegistry")
    tool_output_store = ToolOutputStore(workspace_root=workspace_root)

    def read_artifact(file_id: str, max_bytes: int) -> ResourceFileContent:
        binding = tool_invocation_context.thread_binding
        if binding is None:
            error = PermissionError("产物读取缺少当前 Agent SessionThread")
            raise mark_public_failure(error, str(error))
        parsed = parse_session_artifact_uri(file_id)
        if parsed.tail_segments[0] != binding.session_id:
            error = PermissionError("产物 URI 不属于当前 Agent SessionThread")
            raise mark_public_failure(error, str(error))
        if parsed.session_artifact_kind == "attachments":
            if not isinstance(checkpointer, RolloutCheckpointSaver):
                error = PermissionError("附件读取缺少原 checkpoint owner")
                raise mark_public_failure(error, str(error))
            return checkpointer.read_tool_call_attachment(
                binding.session_id, thread_id=binding.thread_id, file_id=file_id,
                tool_call_id=tool_invocation_context.require_tool_call_id(),
                model_call_id=tool_invocation_context.require_model_call_id(),
            )
        return tool_output_store.read(binding.session_id, file_id, max_bytes)

    backend = build_workspace_backend(workspace_root)
    # CSM 的控制状态必须经唯一 RolloutCheckpointSaver/ContextStore owner 持久化。
    # 生产 checkpointer 就是该 owner；其它（测试替身）checkpointer 没有
    # ContextStore，只能使用无持久端口的 CSM，不伪造持久化 owner。
    context_source_owner: ContextSourceOwnerKey | None = None
    context_source_control_port: ContextSourceControlStatePort | None = None
    if isinstance(checkpointer, RolloutCheckpointSaver):
        candidate_owner = ContextSourceOwnerKey(
            session_id=session_id,
            thread_id=thread_id,
        )
        if checkpointer.context_source_control_owner_available(candidate_owner):
            context_source_owner = candidate_owner
            context_source_control_port = checkpointer
    # compaction preflight 端口：生产 checkpointer 即唯一 Saver owner；
    # 测试替身按合成装配合同拿到显式失败端口，禁止静默跳过。
    compaction_preflight: CompactionPreflightPort = (
        checkpointer
        if isinstance(checkpointer, RolloutCheckpointSaver)
        else NoDurableOwnerCompactionPreflight()
    )
    # OpenSpec 3.8-C：CSM 的 commit/untrack 成功边界发布 context.source/* 轻量
    # 内存事件。CSM 边界拿不到 workspace_id，channel 参数使用最近稳定 identity
    # session_id（与 CSM owner 一致）；事件服务复用文件来源 registry 的进程级
    # channel 服务（ResourcePlatformBootstrap 落地后应由 bootstrap 持有并注入）。
    # 来源确认事件只在真实 Saver seal 成功后发布。
    context_source_event_sink: Callable[[ContextSourceEvent], None] | None = None
    if workspace_file_resource_registry is not None:
        context_source_event_sink = ContextSourceEventPublisher(
            event_service=(
                workspace_file_resource_registry.observation_channel.event_service
            ),
            scope_id=session_id,
        )
    context_source_manager = ContextSourceManager(
        owner=context_source_owner,
        control_state_port=context_source_control_port,
        lifecycle_event_sink=context_source_event_sink,
    )
    # 事件驱动 reaction：reactor 订阅来源 owner 的轻量 change 通知，并把
    # 「有新 revision」标记为 CSM 的 pending observation；before_model 只消费
    # 已排队的 observation，不再遍历 descriptor 轮询内存快照。
    # 订阅必须随 agent 生命周期释放；调用方没有提供 scope 时在代码内留 TODO，
    # 不允许在这里新建第二套 dispose 抽象。
    context_source_reactor: ContextSourceReactor | None = None
    if workspace_file_resource_registry is not None:
        if reactor_lifetime_scope is None:
            raise RuntimeError(
                "启用事件驱动 context source reaction 时必须提供 "
                "reactor_lifetime_scope；否则订阅无法随 agent 生命周期释放"
            )
        context_source_reactor = ContextSourceReactor(
            sources=workspace_file_resource_registry,
            context_sources=context_source_manager,
            lifetime_scope=reactor_lifetime_scope,
            reactor_id=f"agent:{agent_id}:session:{session_id}",
        )
    extension_confirmation_names: set[str] = set()
    direct_confirmation_names: set[str] = set()
    extension_policies: dict[str, ToolCapabilityPolicy] = {}
    custom_metadata: dict[str, ToolMetadata] = {}
    custom_specs_by_name = {spec.name: spec for spec in custom_tool_specs}
    custom_skill_bindings = {
        name: spec.skills for name, spec in custom_specs_by_name.items() if spec.skills
    }
    extension_tools: list[BaseTool] = []
    custom_tools: list[BaseTool] = []
    extension_invoker: BaseTool | None = None

    def _absorb_builtin_tool(tool: BaseTool) -> None:
        """内置工具的唯一准入路径：策略解析 + 可见性/确认登记。

        显式 ``tools`` 与默认工具集两条来源只在「是否过滤 denylist、是否跳过
        非 BaseTool 项」上不同，策略准入与登记必须共用这一条实现。
        """
        policy = capability_resolver.resolve(
            metadata_for_tool(tool, origin="builtin"),
            execution_override=resolved_execution_overrides.get(tool.name),
            model_visibility_override=resolved_model_visibility_overrides.get(tool.name),
        )
        if not policy.execution_enabled:
            return
        resolved_tools.append(tool)
        if policy.confirmation_required:
            direct_confirmation_names.add(tool.name)

    if tools is not None:
        declared_tools = list(tools)
        resolved_tools = []
        for tool in tools:
            if not isinstance(tool, BaseTool):
                resolved_tools.append(tool)
                continue
            _absorb_builtin_tool(tool)
    else:
        if browser_resource_owner is None:
            raise RuntimeError(
                "assemble_deep_agent 构建默认工具集时需要显式传入 BrowserResourceOwner"
            )
        visible_tools = build_default_tools(
            session_id=session_id,
            agent_id=agent_id,
            background_task_registry=background_task_registry,
            background_mailbox=background_mailbox,
            goal_service=goal_service,
            user_question_service=user_question_service,
            user_facing=session_service.path_resolver.main_thread_id(session_id) == thread_id,
            session_subagent_service=session_subagent_service,
            team_service=team_service,
            terminal_resource_owner=terminal_resource_owner,
            invocation_context=tool_invocation_context,
            session_communication_service=session_communication_service,
            include_test_tools=config_service.development_test_tools_enabled(),
            include_team_tools=include_team_tools,
            filesystem_permissions=permissions,
            text_mutations=text_mutations,
        )
        declared_custom_tools = build_custom_tools(
            custom_tool_specs,
            context=CustomToolFactoryContext(
                agent_id=agent_id,
                sender_agent_id=resolved_sender_agent_id,
                workspace_root=workspace_root,
                background_task_registry=background_task_registry,
                background_message_bus=background_mailbox,
                job_event_bus=job_event_bus,
                job_service=job_service,
                session_context_query_service=session_context_query_service,
                workspace_session_context_client=workspace_session_context_client,
                session_communication_service=session_communication_service,
                session_orchestrator=session_orchestrator,
                config_service=config_service,
                terminal_resource_owner=terminal_resource_owner,
                browser_resource_owner=browser_resource_owner,
                invocation_context=tool_invocation_context,
                node_debug_service=node_debug_service,
                mcp_connection_service=mcp_connection_service,
            ),
        )
        declared_tools = [*visible_tools, *declared_custom_tools, *(mcp_tools or ())]
        custom_tools = []
        for tool in declared_custom_tools:
            if tool.name in resolved_tool_denylist:
                continue
            metadata = metadata_for_custom_tool(custom_specs_by_name[tool.name])
            custom_metadata[tool.name] = metadata
            policy = capability_resolver.resolve(
                metadata,
                execution_override=resolved_execution_overrides.get(tool.name),
                model_visibility_override=resolved_model_visibility_overrides.get(tool.name),
            )
            extension_policies[tool.name] = policy
            custom_tools.append(tool)
            if policy.execution_enabled and policy.confirmation_required:
                extension_confirmation_names.add(tool.name)
        mcp_tools_for_agent = []
        for tool in mcp_tools or []:
            if tool.name in resolved_tool_denylist:
                continue
            policy = capability_resolver.resolve(
                metadata_for_tool(tool, origin="mcp"),
                execution_override=resolved_execution_overrides.get(tool.name),
                model_visibility_override=resolved_model_visibility_overrides.get(tool.name),
            )
            extension_policies[tool.name] = policy
            mcp_tools_for_agent.append(tool)
            if policy.execution_enabled and policy.confirmation_required:
                extension_confirmation_names.add(tool.name)
        extension_tools = [
            *custom_tools,
            *mcp_tools_for_agent,
        ]
        resolved_tools = []
        for tool in visible_tools:
            if tool.name in resolved_tool_denylist:
                continue
            _absorb_builtin_tool(tool)
    # 固定信封即使当前没有可执行 target 也必须存在；target 的启停只改变
    # 封存目录与执行准入，不改变 Provider 工具面。
    target_resolver = None
    extension_call_scope = None
    skill_snapshot_resolver = None

    def discovery_catalog_resolver() -> FrozenToolCatalog:
        raise ExtensionCatalogUnavailableError("search_tools 需要调用所属的冻结完整目录")

    def resolve_extension_capabilities(metadata: Sequence[ToolMetadata]) -> dict[str, ToolCapabilityPolicy]:
        selection = tool_selection_reader.snapshot(agent_id)
        execution_overrides = selection.execution_overrides
        visibility_overrides = selection.model_visibility_overrides
        policies = {}
        for entry in metadata:
            policy = capability_resolver.resolve(
                entry, execution_override=execution_overrides.get(entry.tool_id),
                model_visibility_override=visibility_overrides.get(entry.tool_id),
            )
            if entry.tool_id in resolved_tool_denylist:
                policy = replace(policy, execution_enabled=False, model_visible=False)
            policies[entry.tool_id] = policy
        return policies

    if resource_activation is not None:
        if not isinstance(checkpointer, RolloutCheckpointSaver) or mcp_catalog_owner is None:
            raise RuntimeError("正式 runtime 需要唯一 Saver 与真实 MCP Owner")
        if reactor_lifetime_scope is None:
            raise RuntimeError("正式 MCP publication 订阅需要 generation LifetimeScope")

        def enabled_extension(target: BaseTool) -> bool:
            metadata = target.metadata or {}
            origin = "mcp" if metadata.get("mcp_server_id") else "custom"
            group_id = custom_metadata[target.name].group_id if target.name in custom_metadata else None
            return resolve_extension_capabilities((metadata_for_tool(
                target, origin=origin, group_id=group_id,
            ),))[target.name].execution_enabled

        effective_skill_identities = (
            {entry.entry_identity for entry in resolved_skills.entries}
            if resolved_skills is not None else set()
        )
        published_skill_sources = tuple(
            SkillToolSource(
                name=source.name, source_id=source.source_id,
                entry_identity=source.entry_identity, display_uri=source.display_uri,
                plugin=source.plugin, effective=source.entry_identity in effective_skill_identities,
            )
            for source in (resolved_skills.sources if resolved_skills is not None else ())
        )
        publisher = McpCatalogResourcePublisher(
            registry=resource_registry, custom_tools=custom_tools,
            policy_resolver=resolve_extension_capabilities, custom_metadata=custom_metadata,
            custom_skill_bindings=custom_skill_bindings,
            skill_sources=published_skill_sources,
            gateway_id=resolved_skills.gateway_id if resolved_skills is not None else None,
        )
        unsubscribe = mcp_catalog_owner.subscribe_publication(publisher.publish)
        try:
            reactor_lifetime_scope.register(unsubscribe, label="MCP catalog publication")
        except BaseException:
            unsubscribe()
            raise

        def republish_extension_context() -> None:
            # 与 relist 共用原 Owner 发布锁，不能用锁外读到的旧目录覆盖新 publication。
            with mcp_catalog_owner.publication_lock:
                publisher.publish(mcp_catalog_owner.publication_snapshot())

        unsubscribe_selection = tool_selection_reader.subscribe(agent_id, republish_extension_context)
        try:
            reactor_lifetime_scope.register(unsubscribe_selection, label="extension tool selection publication")
        except BaseException:
            unsubscribe_selection()
            raise

        def resolve_skill_snapshot(name: str):
            return checkpointer.load_tool_call_skill_catalog(
                session_id, thread_id=thread_id, skill_name=name,
                tool_call_id=tool_invocation_context.require_tool_call_id(),
                model_call_id=tool_invocation_context.require_model_call_id(),
            )

        skill_snapshot_resolver = resolve_skill_snapshot

        def discovery_catalog_resolver() -> FrozenToolCatalog:
            return checkpointer.load_tool_call_extension_catalog(
                session_id, thread_id=thread_id,
                tool_call_id=tool_invocation_context.require_tool_call_id(),
                model_call_id=tool_invocation_context.require_model_call_id(),
            )

        def resolve_catalog():
            return checkpointer.resolve_extension_dispatch_for_tool_call(
                session_id, thread_id=thread_id,
                tool_call_id=tool_invocation_context.require_tool_call_id(),
                model_call_id=tool_invocation_context.require_model_call_id(),
            ).binding_ref

        def reserve_extension_call(snapshot: ResourceActivationSnapshotRef, model_call_id: str, checkpoint_ns: str):
            parents = tuple(binding for binding in snapshot.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
            if len(parents) != 1:
                raise ExtensionTargetResolutionError("MCP 封存资格缺少唯一冻结目录")
            dispatch = restore_frozen_extension_dispatch(
                snapshot=snapshot, binding=parents[0], model_call_id=model_call_id,
                payload=checkpointer.read_resource_activation_payload(
                    snapshot, parents[0], storage_owner=(session_id, thread_id), checkpoint_ns=checkpoint_ns,
                ),
            )
            execution_scope = None
            if parents[0].effective_boundary == "turn" and any(target.origin == "mcp" for target in dispatch.binding_ref.targets.values()):
                turn_scope = get_current_turn_execution_scope()
                if turn_scope is None:
                    raise ExtensionTargetResolutionError("MCP Turn 冻结目录缺少原 execution scope")
                execution_scope = turn_scope.lifetime_scope
            return mcp_catalog_owner.execution_handles.reserve_model_call(
                dispatch.binding_ref, session_id=session_id, thread_id=thread_id, model_call_id=model_call_id,
                execution_id=snapshot.execution_id if execution_scope is not None else None,
                execution_scope=execution_scope,
            )

        extension_call_scope = reserve_extension_call

        local_targets = {tool.name: tool for tool in custom_tools}

        def resolve_target(binding: ExtensionTargetBinding, use: McpResourceUse | None):
            if binding.origin == "mcp":
                return mcp_catalog_owner.execution_handles.execution_target(binding, use)
            target = local_targets.get(binding.target_id)
            if target is None:
                raise ExtensionTargetResolutionError(
                    f"sealed target 缺少本地执行注册: {binding.target_id}"
                )
            return nullcontext(ExtensionExecutionTarget(target))

        target_resolver = resolve_target
        permission_check = enabled_extension
    else:
        resolve_catalog = create_extension_catalog_resolver_from_tools(extension_tools)
        permission_check = lambda target: bool(
            getattr(extension_policies.get(target.name), "execution_enabled", False)
        )
    extension_invoker = create_extension_tool_invoker_tool(
        extension_tools,
        catalog_binding_resolver=resolve_catalog,
        target_resolver=target_resolver,
        is_tool_execution_enabled=permission_check,
        invocation_context=tool_invocation_context,
    )
    resolved_tools.append(extension_invoker)
    declared_tools.append(extension_invoker)

    def resolve_discovery_capabilities(catalog: FrozenToolCatalog) -> dict[str, ToolCapabilityPolicy]:
        return resolve_extension_capabilities(tuple(definition.metadata for definition in catalog.definitions))

    discovery_tool = create_search_tools_tool(
        catalog_resolver=discovery_catalog_resolver, policy_resolver=resolve_discovery_capabilities,
        tool_output_store=tool_output_store, session_id=session_id,
        invocation_context=tool_invocation_context,
        response_byte_limit=min(tool_output_store.inline_byte_limit, capability_resolver.resolve(
            ToolMetadata(TOOL_DISCOVERY_NAME, "builtin", "default", "default"),
        ).max_result_bytes),
    )
    resolved_tools.append(discovery_tool)
    declared_tools.append(discovery_tool)
    if (
        context_source_manager is not None
        and not any(getattr(tool, "name", None) == "skill_load" for tool in resolved_tools)
    ):
        def skill_control_admission(name: str, mode: SkillLoadMode) -> AbstractAsyncContextManager[None]:
            return session_service.session_context_control_admission(
                session_id, thread_id,
                operation_id=tool_invocation_context.require_operation_id("skill_load"),
                payload={"kind": "skill_load", "name": name, "mode": mode},
                recovery_ref=tool_invocation_context.require_tool_call_id(),
            )

        skill_tool = create_skill_load_tool(
            context_source_manager, activation_snapshot_resolver=skill_snapshot_resolver,
            control_admission=skill_control_admission,
        )
        declared_tools.append(skill_tool)
        if tools is not None or "skill_load" not in resolved_tool_denylist:
            _absorb_builtin_tool(skill_tool)
    if enabled_tool_names is not None:
        resolved_tools = [tool for tool in resolved_tools if getattr(tool, "name", "") in enabled_tool_names]
        for fixed_tool in (extension_invoker, discovery_tool):
            if fixed_tool not in resolved_tools:
                resolved_tools.append(fixed_tool)
    resolved_interrupt_on = dict(interrupt_on or {})
    resolved_interrupt_on.update(
        {tool_name: True for tool_name in direct_confirmation_names}
    )
    resolved_tool_names = {
        getattr(tool, "name", "")
        for tool in resolved_tools
    }
    validate_tool_dependencies(
        resolved_tool_names,
        context=f"agent {agent_id} 的运行时工具策略",
    )
    runtime_middleware: list[AgentMiddleware] = (
        list(middleware) if middleware is not None else [LLMLoggingMiddleware()]
    )
    if enabled_runtime_middleware_names is not None:
        runtime_middleware = [
            item for item in runtime_middleware if item.__class__.__name__ in enabled_runtime_middleware_names
        ]

    # 检查完整声明，隐藏项也不能与中间件或压缩工具重名；不绑定模型实现。
    from app.agents.model_tool_schema import export_model_tool_json_schema

    tool_middleware = build_deep_agent_tool_middleware(
        backend=backend, workspace_root=workspace_root,
        resource_navigation=resource_navigation, text_mutations=text_mutations,
        artifact_reader=read_artifact, permissions=permissions,
    )
    local_tools = [
        *declared_tools,
        *(tool for item in (*tool_middleware, *runtime_middleware) for tool in getattr(item, "tools", ())),
        COMPACT_CONVERSATION_TOOL,
        READ_COMPACTION_HISTORY_TOOL,
    ]
    names = [tool_definition_name(tool) for tool in local_tools]
    if not all(names) or len(names) != len(set(names)):
        raise ValueError("工具注册目录存在空工具名或重复工具名")
    for tool in local_tools:
        if isinstance(tool, BaseTool):
            export_model_tool_json_schema(tool)
    if model is None:
        model_setup = prepare_agent_model(
            agent_id=agent_id, config_service=config_service,
            prompt_cache_key=session_id, preferred_model=preferred_model,
            gateway_id=resolved_skills.gateway_id,
            lifetime_scope=reactor_lifetime_scope,
        )
        model = model_setup.model
        instruction_producers = model_setup.instruction_producers
        model_routing_middleware = model_setup.routing_middleware if model_routing_enabled else None
    elif not instruction_producers:
        from app.agents.instruction_producers import runtime_instruction_producers

        runtime_config = config_service.get_agent_runtime_config(agent_id, preferred_model=preferred_model)
        instruction_producers = runtime_instruction_producers(
            agent_id=agent_id, content=runtime_config["system_prompt"],
            config_revision=config_service.get_revision(),
            runtime_identity_description=runtime_config["runtime_identity_description"],
            provider_candidates=[(provider["provider_id"], provider["model_id"]) for provider in runtime_config["models"]],
        )
    for producer in instruction_producers:
        register_instruction_producer(context_source_manager, producer)
    team_policy_revision = sha256_jcs({"content": TEAM_COORDINATION_SYSTEM_PROMPT})
    register_instruction_producer(context_source_manager, InstructionProducerSpec(
        producer_id="R03", source_id="instruction:team-coordination-policy",
        source_kind="team_coordination_policy", name="团队协作规则",
        root_placement="root_eligible", content=TEAM_COORDINATION_SYSTEM_PROMPT,
        revision=team_policy_revision,
        policy_key="team_coordination",
        update_binding=InstructionUpdateBinding("software_release", team_policy_revision, "epoch"),
    ))

    tool_output_middleware = ToolOutputMiddleware(
        session_id=session_id,
        store=tool_output_store,
    )
    tool_invocation_context_middleware = ToolInvocationContextMiddleware(
        tool_invocation_context
    )

    deepagent_middleware = build_deep_agent_middleware(
        model=model,
        backend=backend,
        tool_middleware=tool_middleware,
        resolved_skills=resolved_skills,
        execution_owned_skill_catalog=resource_activation is not None,
        compaction_preflight=compaction_preflight,
        context_source_manager=context_source_manager,
        source_registry=workspace_file_resource_registry,
        context_source_reactor=context_source_reactor,
        resolved_tool_denylist=resolved_tool_denylist,
        interrupt_on=resolved_interrupt_on,
        runtime_middleware=runtime_middleware,
        model_routing_middleware=model_routing_middleware,
        tool_invocation_context_middleware=tool_invocation_context_middleware,
        tool_output_middleware=tool_output_middleware,
        custom_tool_confirmation_names=frozenset(
            set(custom_tool_confirmation_names) | extension_confirmation_names
        ),
    )

    registered_tools = [
        *resolved_tools,
        *(tool for item in deepagent_middleware for tool in getattr(item, "tools", ())),
    ]
    tool_set_producer = RegisteredToolSetProducer(
        agent_id=agent_id, selection_reader=tool_selection_reader,
        policy_resolver=capability_resolver, tools=registered_tools,
        metadata={
            tool_definition_name(tool): metadata_for_tool(tool, origin="builtin")
            for tool in registered_tools
        },
    )
    # routing 和所有 request producer 的真实 attempt 直接调用唯一执行 owner。
    execution = ModelCallExecution(
        checkpointer=checkpointer, session_id=session_id, thread_id=thread_id,
        context_source_manager=context_source_manager,
        source_message_grouping_supplier=config_service.get_source_message_grouping,
        tool_set_producer=tool_set_producer,
        extension_policy_resolver=resolve_extension_capabilities,
        extension_call_scope=extension_call_scope,
        resource_activation=resource_activation,
        context_read_admission=lambda: session_service.session_read_admission(session_id),
        context_control_admission=lambda operation_id, payload, recovery_ref: session_service.session_context_control_admission(
            session_id, thread_id, operation_id=operation_id,
            payload=payload, recovery_ref=recovery_ref,
        ),
    )
    agent = create_model_call_agent(
        model,
        execution=execution,
        system_prompt=None,
        tools=list(resolved_tools) if resolved_tools else None,
        middleware=deepagent_middleware,
        response_format=response_format,
        context_schema=None,
        checkpointer=checkpointer,
        store=None,
        debug=debug,
        name=name,
        cache=None,
    )

    runtime_agent = agent.with_config(
        {
            "recursion_limit": AGENT_GRAPH_RECURSION_LIMIT,
            "metadata": {
                "ls_integration": "deepagents",
                "versions": {"deepagents": "custom"},
                "lc_agent_name": name,
            },
        }
    )

    # 仅记录真实构建对象的进程内身份；compiled graph 不进入业务库或 trace。
    observe_phase_sync(thread_id, "agent.graph.compiled", details={
        "session_id": session_id, "thread_id": thread_id, "owns_thread": True,
        "compiled_graph_object_id": id(agent),
        "compiled_graph_type": f"{type(agent).__module__}.{type(agent).__qualname__}",
        "agent_object_id": id(runtime_agent),
        "scope_object_id": id(reactor_lifetime_scope) if reactor_lifetime_scope is not None else None,
    })
    return runtime_agent
