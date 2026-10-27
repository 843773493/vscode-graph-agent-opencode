from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Self

from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.abstractions.communication import SessionCommunicationProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.resource_file_content import ResourceFileContent
from app.abstractions.session_context import WorkspaceSessionContextClientProtocol
from app.abstractions.session_subagent import SessionSubagentProtocol
from app.abstractions.session_target import SessionTargetResolverProtocol
from app.abstractions.team import TeamCoordinationProtocol
from app.agents.context_checkpoint_store import ContextCompactionCheckpointStore
from app.agents.context_compaction_adapter import AgentSummarizationCompactor
from app.agents.graph_binding import SessionControlGraphBindingStore
from app.agents.workspace_backend import build_workspace_backend
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.env import get_project_root
from app.core.job_event_bus import JobEventBus
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.path_utils import (
    SessionCatalogComponents,
    build_session_catalog_components,
    catalog_provider_scope,
    get_session_catalog_components,
    get_user_config_root,
    workspace_context,
)
from app.core.phase_observer import observe_phase
from app.core.resource_uri import parse_resource_uri
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.auth import get_gateway_local_token
from app.runtime.agent_runtime import AgentRuntimeDependencyProvider
from app.runtime.runtime_ports import DEFAULT_RUNTIME_CLOCK_PORTS, RuntimeClockPorts
from app.runtime.session_orchestrator import SessionOrchestrator
from app.services.business.agent_service import AgentService
from app.services.business.browser_resources.owner import BrowserResourceOwner
from app.services.business.context_compaction_service import ContextCompactionService
from app.services.business.gateway_context_query_service import (
    GatewayContextQueryService,
)
from app.services.business.job.service import JobService
from app.services.business.message_service import MessageService
from app.services.business.session_changes_service import SessionChangesService
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_context_query_service import (
    SessionContextQueryService,
)
from app.services.business.session_generation import (
    AgentPromptGenerationProvider,
    SessionGenerationService,
)
from app.services.business.session_goal_service import SessionGoalService
from app.services.business.session_information_service import SessionInformationService
from app.services.business.session_interrupt_service import SessionInterruptService
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_resource_providers import (
    BackgroundTaskResourceProvider,
    BrowserResourceProvider,
    TerminalResourceProvider,
)
from app.services.business.session_resource_registry import (
    SessionResourceProviderRegistry,
)
from app.services.business.session_resource_service import SessionResourceService
from app.services.business.session_service import SessionService
from app.services.business.session_skill_tracking_service import (
    SessionSkillTrackingService,
)
from app.services.business.session_target_resolver import SessionTargetResolver
from app.services.business.session_turn_history import (
    SessionTurnHistoryService,
)
from app.services.business.session_turn_replay_service import SessionTurnReplayService
from app.services.business.team.service import TeamCoordinationService
from app.services.business.terminal_resources.owner import TerminalResourceOwner
from app.services.event_service import EventService
from app.services.infrastructure.artifact_service import ArtifactService
from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    ATTACHMENT_ROOT_DIRECTORY_NAME,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceLock,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.background_work_handlers import (
    SystemTimeEmitterHandler,
)
from app.services.infrastructure.background_work_store import BackgroundWorkStore
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.config import WorkspaceSourceOwner
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.events.channel_events import (
    ResourceStateEventPublisher,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.file_tree_settings_service import (
    FileTreeSettingsService,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.gateway_communication_client import (
    GatewayCommunicationClient,
)
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from app.services.infrastructure.llm_request_log_service import LLMRequestLogService
from app.services.infrastructure.log_service import LogService
from app.services.infrastructure.mcp import McpCatalogOwner
from app.services.infrastructure.mcp.connection_service import McpConnectionService
from app.services.infrastructure.message_stream_store import MessageStreamStore
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session.fork import (
    build_workspace_fork_config,
)
from app.services.infrastructure.node_debug.session.session_admission import (
    NodeDebugSessionAdmission,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.pending_request_store import PendingRequestStore
from app.services.infrastructure.resource_platform.bootstrap import (
    ResourcePlatform,
    bootstrap_resource_platform,
)
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_checkpoint_runtime import (
    RolloutCheckpointRuntime,
)
from app.services.infrastructure.rollout_context.activation.body import (
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.infrastructure.session_changes_store import SessionChangesStore
from app.services.infrastructure.session_goal_store import SessionGoalStore
from app.services.infrastructure.session_message_idempotency_store import (
    SessionMessageIdempotencyStore,
)
from app.services.infrastructure.team.store import TeamStore
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from app.services.infrastructure.tool_catalog_service import ToolCatalogService
from app.services.infrastructure.tool_output_store import ToolOutputStore
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from app.services.infrastructure.tool_service import ToolService
from app.services.infrastructure.trace_event_recorder import TraceEventRecorder
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)
from app.services.infrastructure.workspace_files.gateway_text_mutation import (
    GatewayTextMutation,
)
from app.services.infrastructure.workspace_files.paths import (
    GatewayNativeDirectoryClient,
    WorkspaceFilePaths,
)
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from app.services.infrastructure.workspace_service import WorkspaceService
from app.services.infrastructure.workspace_state_store import WorkspaceActivityService
from app.services.mapping.session_resource_mapper import SessionResourceMapper
from app.services.orchestration.agent_execution_service import AgentExecutionService
from app.services.orchestration.communication.service import (
    SessionCommunicationService,
)
from app.services.orchestration.goal_runtime_service import GoalRuntimeService
from app.services.orchestration.job_execution_service import JobExecutionService
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)
from app.services.orchestration.resource_activation import ResourceActivationCoordinator
from app.services.orchestration.runtime_source_dispatcher import RuntimeSourceDispatcher
from app.services.orchestration.session_subagent_service import (
    SessionSubagentService,
)
from app.services.orchestration.session_title_service import SessionTitleService
from app.services.orchestration.team.fanout_service import TeamFanoutService
from app.services.orchestration.terminal_steering_service import TerminalSteeringService
from app.services.orchestration.thread_initial_execution_service import (
    ThreadInitialExecutionService,
)
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from app.tool_testing import ToolTestRegistry, ToolTestService, ToolTestStore


class _AgentRuntimeDependencyProvider(AgentRuntimeDependencyProvider):
    def __init__(
        self,
        *,
        message_service: MessageService,
        session_service: SessionService,
        checkpointer: BaseCheckpointSaver,
        terminal_resource_owner: TerminalResourceOwner,
        browser_resource_owner: BrowserResourceOwner,
        session_context_query_service: SessionContextQueryService,
        workspace_session_context_client: WorkspaceSessionContextClientProtocol,
        session_target_resolver: SessionTargetResolverProtocol,
        mcp_catalog_owner: McpCatalogOwner,
        mcp_connection_service: McpConnectionService,
        resource_navigation: ResourceNavigation,
        text_mutations: WorkspaceTextMutationOwner,
    ) -> None:
        self._message_service = message_service
        self._session_service = session_service
        self._checkpointer = checkpointer
        self._terminal_resource_owner = terminal_resource_owner
        self._browser_resource_owner = browser_resource_owner
        self._session_context_query_service = session_context_query_service
        self._workspace_session_context_client = workspace_session_context_client
        self._session_target_resolver = session_target_resolver
        self._mcp_catalog_owner = mcp_catalog_owner
        self._mcp_connection_service = mcp_connection_service
        self._resource_navigation = resource_navigation
        self._text_mutations = text_mutations
        self._node_debug_service: NodeDebugService | None = None
        self._job_service: JobServiceProtocol | None = None
        self._session_orchestrator: SessionOrchestrator | None = None
        self._session_communication_service: SessionCommunicationProtocol | None = None
        self._session_subagent_service: SessionSubagentProtocol | None = None
        self._team_service: TeamCoordinationProtocol | None = None
        self._goal_service: SessionGoalService | None = None

    def set_goal_service(self, goal_service: SessionGoalService) -> None:
        self._goal_service = goal_service

    def get_goal_service(self) -> SessionGoalService:
        if self._goal_service is None:
            raise RuntimeError("_AgentRuntimeDependencyProvider 未绑定 SessionGoalService")
        return self._goal_service

    def get_message_service(self) -> MessageService:
        return self._message_service

    def get_session_service(self) -> SessionService:
        return self._session_service

    def get_checkpointer(self) -> BaseCheckpointSaver:
        return self._checkpointer

    def get_resource_navigation(self) -> ResourceNavigation:
        return self._resource_navigation

    def get_text_mutations(self) -> WorkspaceTextMutationOwner:
        return self._text_mutations

    def get_terminal_resource_owner(self) -> TerminalResourceOwner:
        return self._terminal_resource_owner

    def get_browser_resource_owner(self) -> BrowserResourceOwner:
        return self._browser_resource_owner

    def get_session_context_query_service(self) -> SessionContextQueryService:
        return self._session_context_query_service

    def get_workspace_session_context_client(
        self,
    ) -> WorkspaceSessionContextClientProtocol:
        return self._workspace_session_context_client

    def get_session_target_resolver(self) -> SessionTargetResolverProtocol:
        return self._session_target_resolver

    def set_session_communication_service(
        self,
        service: SessionCommunicationProtocol,
    ) -> None:
        self._session_communication_service = service

    def get_session_communication_service(self) -> SessionCommunicationProtocol:
        if self._session_communication_service is None:
            raise RuntimeError(
                "_AgentRuntimeDependencyProvider 未绑定 SessionCommunicationService"
            )
        return self._session_communication_service

    def get_mcp_catalog_owner(self) -> McpCatalogOwner:
        return self._mcp_catalog_owner

    def get_mcp_connection_service(self) -> McpConnectionService:
        return self._mcp_connection_service

    def get_mcp_tools(self) -> list[BaseTool]:
        return self._mcp_catalog_owner.get_tools()

    def set_node_debug_service(self, node_debug_service: NodeDebugService) -> None:
        self._node_debug_service = node_debug_service

    def get_node_debug_service(self) -> NodeDebugService:
        if self._node_debug_service is None:
            raise RuntimeError(
                "_AgentRuntimeDependencyProvider 未绑定 NodeDebugService"
            )
        return self._node_debug_service

    def set_job_service(self, job_service: JobServiceProtocol) -> None:
        self._job_service = job_service

    def get_job_service(self) -> JobServiceProtocol:
        if self._job_service is None:
            raise RuntimeError("_AgentRuntimeDependencyProvider 未绑定 JobService")
        return self._job_service

    def set_session_orchestrator(self, session_orchestrator: SessionOrchestrator) -> None:
        self._session_orchestrator = session_orchestrator

    def get_session_orchestrator(self) -> SessionOrchestrator:
        if self._session_orchestrator is None:
            raise RuntimeError("_AgentRuntimeDependencyProvider 未绑定 SessionOrchestrator")
        return self._session_orchestrator

    def set_session_subagent_service(
        self,
        session_subagent_service: SessionSubagentProtocol,
    ) -> None:
        self._session_subagent_service = session_subagent_service

    def get_session_subagent_service(self) -> SessionSubagentProtocol:
        if self._session_subagent_service is None:
            raise RuntimeError("_AgentRuntimeDependencyProvider 未绑定 SessionSubagentService")
        return self._session_subagent_service

    def set_team_service(self, team_service: TeamCoordinationProtocol) -> None:
        self._team_service = team_service

    def get_team_service(self) -> TeamCoordinationProtocol:
        if self._team_service is None:
            raise RuntimeError("_AgentRuntimeDependencyProvider 未绑定 TeamCoordinationService")
        return self._team_service


@dataclass(slots=True)
class AppContainer:
    workspace_id: str
    workspace_root: Path
    session_catalog: SessionCatalogComponents
    config_service: ConfigService
    workspace_source_owner: WorkspaceSourceOwner
    agent_service: AgentService
    artifact_service: ArtifactService
    event_service: EventService
    job_service: JobServiceProtocol
    message_service: MessageService
    attachment_blob_store: AttachmentBlobStore
    external_resource_leases: ExternalResourceLeaseLedger
    runtime_service: RuntimeService
    goal_service: SessionGoalService
    goal_runtime_service: GoalRuntimeService
    session_interrupt_service: SessionInterruptService
    session_skill_tracking_service: SessionSkillTrackingService
    session_context_fork_service: SessionContextForkService
    session_turn_replay_service: SessionTurnReplayService
    session_turn_history_service: SessionTurnHistoryService
    session_changes_service: SessionChangesService
    session_information_service: SessionInformationService
    session_context_query_service: SessionContextQueryService
    workspace_session_context_transport: GatewaySessionContextClient
    session_resource_service: SessionResourceService
    terminal_resource_owner: TerminalResourceOwner
    browser_resource_owner: BrowserResourceOwner
    context_compaction_service: ContextCompactionService
    session_service: SessionService
    session_orchestrator: SessionOrchestrator
    session_communication_service: SessionCommunicationService
    runtime_source_dispatcher: RuntimeSourceDispatcher
    thread_initial_execution_service: ThreadInitialExecutionService
    terminal_steering_service: TerminalSteeringService
    session_catalog_service: SessionCatalogService
    session_generation_service: SessionGenerationService
    session_subagent_service: SessionSubagentService
    team_service: TeamCoordinationService
    team_fanout_service: TeamFanoutService
    llm_request_log_service: LLMRequestLogService
    log_service: LogService
    tool_service: ToolService
    tool_test_service: ToolTestService
    tool_selection_store: ToolSelectionStore
    workspace_service: WorkspaceService
    resource_platform: ResourcePlatform
    resource_navigation: ResourceNavigation
    text_mutations: WorkspaceTextMutationOwner
    workspace_file_watch_service: WorkspaceFileWatchService
    workspace_file_resource_registry: WorkspaceFileResourceRegistry
    file_tree_settings_service: FileTreeSettingsService
    agent_execution_service: AgentExecutionService
    node_debug_service: NodeDebugService
    job_event_bus: JobEventBusProtocol
    thread_residency_tracker: ThreadResidencyTracker
    background_task_registry: BackgroundTaskRegistry
    background_message_bus: BackgroundMessageBus
    trace_event_store: TraceEventStore
    trace_event_recorder: TraceEventRecorder
    rollout_checkpoint_runtime: RolloutCheckpointRuntime
    checkpointer: RolloutCheckpointSaver
    mcp_catalog_owner: McpCatalogOwner
    mcp_connection_service: McpConnectionService
    pending_request_store: PendingRequestStore
    workspace_activity_service: WorkspaceActivityService
    message_stream_store: MessageStreamStore


class WorkspaceContainerRegistry:
    """缓存按 workspace_id 装配的完整后端服务图。"""

    def __init__(
        self,
        *,
        mounted_workspaces: MountedWorkspaceRegistry,
        project_root: str | Path | None = None,
        clock_ports: RuntimeClockPorts = DEFAULT_RUNTIME_CLOCK_PORTS,
    ) -> None:
        self._mounted_workspaces = mounted_workspaces
        self._project_root = project_root
        self._clock_ports = clock_ports
        self._containers: dict[str, AppContainer] = {}
        self._catalog_components: dict[str, SessionCatalogComponents] = {}
        self._closed = False
        self._lock = RLock()

    @property
    def mounted_workspaces(self) -> MountedWorkspaceRegistry:
        return self._mounted_workspaces

    @property
    def workspace_ids(self) -> tuple[str, ...]:
        return self._mounted_workspaces.workspace_ids

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("WorkspaceContainerRegistry 已关闭，禁止重新装配")

    def get_catalog_components(
        self, sessions_root: Path, workspace_id: str
    ) -> SessionCatalogComponents:
        with self._lock:
            self._require_open()
            mount = self._mounted_workspaces.resolve(workspace_id)
            expected_root = mount.workspace_root / ".boxteam" / "sessions"
            if sessions_root != expected_root:
                raise RuntimeError(
                    "Session catalog 根目录与已挂载工作区不一致: "
                    f"workspace_id={workspace_id}, requested={sessions_root}, "
                    f"mounted={expected_root}"
                )
            components = self._catalog_components.get(workspace_id)
            if components is None:
                components = build_session_catalog_components(sessions_root, workspace_id)
                self._catalog_components[workspace_id] = components
            return components.get_catalog_components(sessions_root, workspace_id)

    def get(self, workspace_id: str) -> AppContainer:
        with self._lock:
            self._require_open()
            mount = self._mounted_workspaces.resolve(workspace_id)
            container = self._containers.get(mount.workspace_id)
            if container is None:
                try:
                    with catalog_provider_scope(self):
                        container = build_app_container(
                            project_root=self._project_root,
                            workspace_root=mount.workspace_root,
                            startup_mount=mount,
                            clock_ports=self._clock_ports,
                        )
                    components = self.get_catalog_components(
                        mount.workspace_root / ".boxteam" / "sessions", mount.workspace_id
                    )
                    if (
                        container.workspace_id != mount.workspace_id
                        or container.session_catalog is not components
                    ):
                        raise RuntimeError(
                            "服务图未使用已挂载工作区唯一 catalog owner: "
                            f"mounted={mount.workspace_id}, container={container.workspace_id}"
                        )
                    self._containers[mount.workspace_id] = container
                except BaseException as error:
                    components = self._catalog_components.pop(mount.workspace_id, None)
                    if components is not None:
                        try:
                            components.close()
                        except Exception as close_error:  # noqa: BLE001 - 关闭失败须与原始失败一起暴露。
                            raise BaseExceptionGroup(
                                "工作区服务图装配失败且 catalog 关闭失败",
                                [error, close_error],
                            ) from None
                    raise
            return container

    def values(self) -> tuple[AppContainer, ...]:
        return tuple(self.get(workspace_id) for workspace_id in self.workspace_ids)

    def close(self) -> None:
        """所有writer排空后关闭catalog；失败保留原组件供同入口重试。"""
        with self._lock:
            self._closed = True
            errors: list[Exception] = []
            for workspace_id, components in tuple(self._catalog_components.items()):
                try:
                    components.close()
                except Exception as error:  # noqa: BLE001 - 其它mount独立关闭，失败项保留。
                    errors.append(error)
                else:
                    del self._catalog_components[workspace_id]
                    self._containers.pop(workspace_id, None)
            if errors:
                raise ExceptionGroup("工作区 catalog owner 关闭失败", errors)
            self._containers.clear()

    def __enter__(self) -> Self:
        with self._lock:
            self._require_open()
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except Exception as close_error:
            if error is not None:
                raise BaseExceptionGroup(
                    "工作区生命周期失败且 catalog 关闭失败", [error, close_error]
                ) from None
            raise


def _build_app_container(
    *,
    project_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
    startup_mount: MountedWorkspace | None = None,
    clock_ports: RuntimeClockPorts = DEFAULT_RUNTIME_CLOCK_PORTS,
    construction: ExitStack,
) -> AppContainer:
    resolved_project_root = get_project_root(project_root)
    if workspace_root is None:
        raise ValueError("build_app_container 必须接收显式 workspace_root")
    resolved_workspace_root = Path(workspace_root).expanduser().resolve(strict=True)
    resolved_boxteam_root = resolved_workspace_root / ".boxteam"
    resolved_sessions_root = resolved_boxteam_root / "sessions"
    workspace_source_owner = WorkspaceSourceOwner(
        path=get_user_config_root() / "workspace-source.sqlite",
    )
    workspace_id = load_or_create_workspace_id(resolved_workspace_root)
    workspace_activity_service = WorkspaceActivityService(
        workspace_root=resolved_workspace_root,
    )
    construction.callback(workspace_activity_service.close)
    session_catalog = get_session_catalog_components(
        resolved_sessions_root, workspace_id=workspace_id
    )
    session_path_resolver = session_catalog.resolver
    session_creation_service = session_catalog.creation_service
    session_path_resolver.initialize()
    job_event_bus = JobEventBus()
    thread_residency_tracker = ThreadResidencyTracker(
        clock=clock_ports.residency, wall_clock=clock_ports.wall,
    )
    background_work_store = BackgroundWorkStore(sessions_dir=resolved_sessions_root)
    background_task_registry = BackgroundTaskRegistry(
        store=background_work_store, residency_tracker=thread_residency_tracker,
        handlers=(SystemTimeEmitterHandler(),),
    )
    background_message_bus = BackgroundMessageBus(store=background_work_store)
    trace_event_store = TraceEventStore(sessions_dir=resolved_sessions_root)
    trace_event_recorder = TraceEventRecorder(
        bus=job_event_bus,
        store=trace_event_store,
    )
    message_stream_store = MessageStreamStore(
        path_resolver=session_path_resolver,
        workspace_id=workspace_id,
    )
    # resource.state producer：账本在 settle/reconcile 等语义上等于「owner 已核实
    # 资源终态」的转换点发布轻量状态；通知失败不回滚 durable 账本事实。
    external_resource_state_events = ResourceStateEventPublisher(
        event_service=job_event_bus.event_channel_service,
        owner_domain="external_resource_leases",
    )
    external_resource_leases = ExternalResourceLeaseLedger(
        state_path=resolved_boxteam_root / "resources.json",
        state_events=external_resource_state_events,
    )
    def native_directories() -> GatewayNativeDirectoryClient:
        return GatewayNativeDirectoryClient(
            workspace_id=workspace_id,
            connection=GatewayTransportConnection(
                gateway_url=os.environ.get("BOXTEAM_GATEWAY_URL"), config_service=config_service,
            ),
            local_token_supplier=get_gateway_local_token,
        )

    def resolve_instruction_file(uri: str) -> tuple[Path, str]:
        return WorkspaceFilePaths(
            workspace=resolved_workspace_root, workspace_id=workspace_id,
            bindings=native_directories(),
        ).resolve(uri, allow_empty=False, reject_symlink=True)

    config_service = ConfigService(
        workspace_root=resolved_workspace_root,
        workspace_state_store=workspace_activity_service.store,
        source_owner=workspace_source_owner,
        source_owner_workspace_id=workspace_id,
        event_channel_service=job_event_bus.event_channel_service,
        startup_mount=startup_mount,
        instruction_file_resolver=resolve_instruction_file,
    )
    attachment_blob_store = AttachmentBlobStore(
        resolved_workspace_root, path_resolver=session_path_resolver, clock=clock_ports.wall,
    )
    construction.callback(attachment_blob_store.close)
    workspace_service = WorkspaceService(
        config_service=config_service,
        workspace_root=resolved_workspace_root,
        attachments=attachment_blob_store,
        native_directories=native_directories(),
    )
    terminal_manager_client = TerminalManagerClient(
        config_service=config_service,
        workspace_id=workspace_id,
        backend_url=startup_mount.terminal_backend_url if startup_mount is not None else None,
    )
    browser_manager_client = BrowserManagerClient(workspace_id=workspace_id, config_service=config_service,
        backend_url=startup_mount.browser_backend_url if startup_mount is not None else None)
    workspace_session_context_transport = GatewaySessionContextClient(
        config_service=config_service,
        local_token_supplier=get_gateway_local_token,
    )
    workspace_session_context_client = GatewayContextQueryService(
        transport=workspace_session_context_transport
    )
    communication_transport = GatewayCommunicationClient(
        config_service=config_service,
    )

    rollout_checkpoint_runtime = RolloutCheckpointRuntime(
        sessions_dir=resolved_sessions_root,
        attachment_store=attachment_blob_store,
        node_debug_workspace_config=lambda: build_workspace_fork_config(
            resolved_workspace_root,
            config_service.get_debug_runtime_config(),
        ),
    )
    checkpointer = rollout_checkpoint_runtime.saver
    resource_activation_body = ResourceActivationBodyStore(
        checkpointer._storage, checkpointer._detail_store,
    )

    mcp_catalog_owner = McpCatalogOwner(
        raw_config=config_service.get_mcp_config(),
        workspace_root=resolved_workspace_root,
        event_service=job_event_bus.event_channel_service,
        leases=external_resource_leases,
        publication_lock=config_service.publication_lock,
    )
    config_service.set_mcp_cleanup_provider(mcp_catalog_owner.cleanup_diagnostics)
    mcp_connection_service = McpConnectionService(
        owner=mcp_catalog_owner, store=workspace_activity_service.store, workspace_id=workspace_id,
    )
    message_service = MessageService(
        main_thread_resolver=session_path_resolver.main_thread_id,
        checkpointer=checkpointer,
        attachment_store=attachment_blob_store,
        canonical_item_reader=checkpointer.read_canonical_items,
        workspace_root=resolved_workspace_root,
    )
    session_service = SessionService(
        config_service=config_service,
        trace_event_store=trace_event_store,
        workspace_id=workspace_service.workspace_id,
        path_resolver=session_path_resolver,
        creation_service=session_creation_service,
    )
    file_tree_settings_service = FileTreeSettingsService(
        workspace_root=resolved_workspace_root,
        path_resolver=session_path_resolver,
        file_paths=workspace_service.file_paths,
    )
    session_service.register_change_listener(
        file_tree_settings_service.handle_session_change
    )
    goal_service = SessionGoalService(
        store=SessionGoalStore(session_path_resolver),
        session_service=session_service,
        job_event_bus=job_event_bus,
    )
    session_context_query_service = SessionContextQueryService(
        message_source=message_service,
        session_lookup=session_service,
        assembly_source=checkpointer,
        workspace_id=workspace_id,
    )
    session_target_resolver = SessionTargetResolver(
        session_service=session_service,
        workspace_session_context_client=workspace_session_context_client,
    )
    thread_creation_factory = OwnerThreadCreationFactory(
        sessions_root=resolved_sessions_root,
        workspace_id=workspace_id,
        path_resolver=session_path_resolver
        if isinstance(session_path_resolver, SessionCatalogPathResolver)
        else None,
    )
    session_changes_store = SessionChangesStore(workspace_root=resolved_workspace_root)
    session_changes_service = SessionChangesService(
        session_service=session_service,
        store=session_changes_store,
    )
    tool_selection_store = ToolSelectionStore(boxteam_root=resolved_boxteam_root)
    artifact_outputs = ToolOutputStore(workspace_root=resolved_workspace_root)

    def read_artifact(uri: str, max_bytes: int) -> ResourceFileContent:
        artifact = parse_resource_uri(uri)
        if artifact.attachment_id is not None:
            session_id = artifact.tail_segments[0] if artifact.session_artifact_kind else None
            return attachment_blob_store.read(session_id, uri)
        if artifact.session_artifact_kind != "tool-results":
            raise ValueError("产物读取必须指向附件或工具结果")
        return artifact_outputs.read(artifact.tail_segments[0], uri, max_bytes)

    resource_platform = bootstrap_resource_platform(
        workspace_root=resolved_workspace_root,
        project_root=resolved_project_root,
        event_service=job_event_bus.event_channel_service,
        workspace_id=workspace_id,
        gateway_connection=GatewayTransportConnection(
            gateway_url=os.environ.get("BOXTEAM_GATEWAY_URL"), config_service=config_service,
        ),
        gateway_token_supplier=get_gateway_local_token,
        file_paths=workspace_service.file_paths,
        artifact_reader=read_artifact,
        gateway_skill_updates=config_service.get_gateway_skill_update_policy(),
    )
    workspace_file_watch_service = resource_platform.file_watch_service
    workspace_file_resource_registry = resource_platform.file_registry
    resource_navigation = resource_platform.navigation
    if resource_navigation is None:
        raise RuntimeError("Workspace 资源导航未绑定显式身份")
    text_mutations = WorkspaceTextMutationOwner(paths=workspace_service.file_paths, gateway=GatewayTextMutation(
        workspace_id=workspace_id,
        connection=GatewayTransportConnection(gateway_url=os.environ.get("BOXTEAM_GATEWAY_URL"), config_service=config_service),
        local_token_supplier=get_gateway_local_token,
    ))
    terminal_resource_owner = TerminalResourceOwner(
        working_directories=attachment_blob_store,
        file_paths=workspace_service.file_paths,
        sessions=session_service, threads=session_path_resolver,
        transport=terminal_manager_client, leases=external_resource_leases,
        thread_release_fence=thread_residency_tracker.is_unloading_generation,
    )
    browser_resource_owner = BrowserResourceOwner(
        workspace_root=resolved_workspace_root, workspace_id=workspace_id,
        sessions=session_service, threads=session_path_resolver,
        transport=browser_manager_client, leases=external_resource_leases,
        thread_release_fence=thread_residency_tracker.is_unloading_generation,
    )
    session_context_fork_service = SessionContextForkService(
        session_service=session_service,
        checkpointer=checkpointer,
        thread_creation_factory=thread_creation_factory,
        goal_service=goal_service,
        terminal_resource_owner=terminal_resource_owner,
        browser_resource_owner=browser_resource_owner,
        transport=workspace_session_context_transport,
    )
    dependency_provider = _AgentRuntimeDependencyProvider(
        message_service=message_service,
        session_service=session_service,
        checkpointer=checkpointer,
        terminal_resource_owner=terminal_resource_owner,
        browser_resource_owner=browser_resource_owner,
        session_context_query_service=session_context_query_service,
        workspace_session_context_client=workspace_session_context_client,
        session_target_resolver=session_target_resolver,
        mcp_catalog_owner=mcp_catalog_owner,
        resource_navigation=resource_navigation,
        mcp_connection_service=mcp_connection_service,
        text_mutations=text_mutations,
    )
    dependency_provider.set_goal_service(goal_service)
    # ThreadResidency：30 分钟 idle 卸载判定器（OpenSpec 2.8/8.8-A）。debug 服务把
    # 已核实的 claim 相位单向推送为 thread 的 idle blocker；tracker 评估时再通过
    # ResidencyBlockerSource 拉取磁盘 durable claim 兜底（重启恢复），不建第二套
    # dispose/LifetimeScope 抽象；tracker 直接持有真实 thread generation scope。
    thread_residency_tracker.add_unload_listener(terminal_resource_owner.release_thread)
    thread_residency_tracker.add_unload_listener(browser_resource_owner.release_thread)
    thread_residency_tracker.add_acquire_listener(attachment_blob_store.bind_thread_runtime)

    async def release_attachment_runtime(session_id: str, thread_id: str, generation: int) -> None:
        attachment_blob_store.release_runtime_binding(
            session_id, thread_id, thread_residency_tracker.runtime_binding_token(generation), messages=checkpointer,
        )

    thread_residency_tracker.add_unload_listener(release_attachment_runtime)
    node_debug_service = NodeDebugService(
        workspace_root=resolved_workspace_root,
        file_paths=workspace_service.file_paths,
        config_service=config_service,
        session_store=NodeDebugSessionStore(session_path_resolver),
        session_admission=NodeDebugSessionAdmission(
            session_service=session_service,
            path_resolver=session_path_resolver,
        ),
        # 与 Agent 执行共用唯一 external_resource_leases 账本：调试进程以 typed
        # node_debug_process 身份登记跨 Turn 占用，不新建第二套 lease 管理。
        external_resource_leases=external_resource_leases,
        state_events=ResourceStateEventPublisher(
            event_service=job_event_bus.event_channel_service,
            owner_domain="node_debug",
        ),
        residency_tracker=thread_residency_tracker,
    )
    # 重启恢复兜底：tracker 评估时向 debug owner 拉取 durable claim 的活跃 blocker。
    thread_residency_tracker.add_blocker_source(node_debug_service)
    dependency_provider.set_node_debug_service(node_debug_service)
    agent_execution_service = AgentExecutionService(
        workspace_id=workspace_id,
        session_catalog=session_catalog,
        config_service=config_service,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        dependency_provider=dependency_provider,
        session_changes_service=session_changes_service,
        tool_selection_store=tool_selection_store,
        message_stream_store=message_stream_store,
        workspace_root=resolved_workspace_root,
        external_resource_leases=external_resource_leases,
        workspace_file_resource_registry=workspace_file_resource_registry,
        gateway_skill_sources=resource_platform.gateway_skill_sources,
        http_skill_sources=resource_platform.http_skill_sources,
        graph_binding_store=SessionControlGraphBindingStore(
            session_control_store_factory=lambda session_id: SessionControlStore(
                session_path_resolver.resolve_session_node(session_id)
                / CONTROL_DATABASE_NAME
            ),
        ),
        # 精确 thread 的真实 generation scope、lease 与 idle unload 共用同一 owner。
        residency_tracker=thread_residency_tracker,
        resource_activation_factory=lambda registry: ResourceActivationCoordinator(
            registry=registry, saver=checkpointer, body_store=resource_activation_body,
        ),
        team_resource_subscriber=lambda session_id, thread_id, registry, scope: team_fanout_service.subscribe(
            session_id, thread_id, registry, scope,
        ),
    )
    session_catalog_service = SessionCatalogService(
        session_service=session_service,
        background_task_registry=background_task_registry,
    )
    session_title_service = SessionTitleService(
        session_service=session_service,
        session_catalog_service=session_catalog_service,
        job_event_bus=job_event_bus,
    )
    job_executor = JobExecutionService(
        agent_execution_service=agent_execution_service,
        message_service=message_service,
        job_event_bus=job_event_bus,
        session_title_service=session_title_service,
    )
    pending_request_store = PendingRequestStore(
        sessions_dir=resolved_sessions_root, owner=checkpointer,
        input_reader=lambda reference: message_service.accepted_job_message(reference, display=False),
    )
    job_service = JobService(
        job_event_bus=job_event_bus,
        job_executor=job_executor,
        pending_request_store=pending_request_store,
        session_lifecycle_guard=session_service.assert_session_active,
        job_timeout_seconds=config_service.get_agent_run_timeout_seconds(),
        job_timeout_seconds_provider=config_service.get_agent_run_timeout_seconds,
        terminal_status_writer=checkpointer,
        input_owner=message_service,
    )
    session_service.bind_job_service(job_service)
    session_catalog_service.bind_job_service(job_service)
    session_turn_history_service = SessionTurnHistoryService(
        checkpointer=checkpointer,
        session_service=session_service,
        job_service=job_service,
        trace_event_store=trace_event_store,
    )
    dependency_provider.set_job_service(job_service)
    runtime_source_dispatcher = RuntimeSourceDispatcher(
        saver=checkpointer, session_service=session_service, job_service=job_service,
    )
    session_orchestrator = SessionOrchestrator(
        attachment_holder=attachment_blob_store.deletion_holder,
        message_service=message_service,
        session_service=session_service,
        config_service=config_service,
        job_service=job_service,
        job_event_bus=job_event_bus,
        runtime_source_dispatcher=runtime_source_dispatcher,
        idempotency_store=SessionMessageIdempotencyStore(
            path_resolver=session_path_resolver,
        ),
    )
    dependency_provider.set_session_orchestrator(session_orchestrator)
    session_communication_service = SessionCommunicationService(
        sessions=session_service, saver=checkpointer, dispatcher=runtime_source_dispatcher,
        transport=communication_transport, query_service=session_context_query_service,
        wait_clock=clock_ports.wait, deadline_timer=clock_ports.deadline_timer,
    )
    dependency_provider.set_session_communication_service(session_communication_service)
    terminal_steering_service = TerminalSteeringService(
        terminal_client=terminal_resource_owner,
        session_orchestrator=session_orchestrator,
    )
    session_generation_service = SessionGenerationService(
        workspace_root=resolved_workspace_root,
        session_service=session_service,
        session_catalog_service=session_catalog_service,
        session_context_fork_service=session_context_fork_service,
        session_orchestrator=session_orchestrator,
        job_event_bus=job_event_bus,
        message_service=message_service,
        trace_event_store=trace_event_store,
        providers=[AgentPromptGenerationProvider()],
    )
    thread_initial_execution_service = ThreadInitialExecutionService(
        factory=thread_creation_factory, sessions=session_service, saver=checkpointer,
        jobs=job_service, dispatcher=runtime_source_dispatcher,
    )
    session_subagent_service = SessionSubagentService(
        parent_session_reader=session_service, thread_creation_factory=thread_creation_factory,
        initial_execution_binder=thread_initial_execution_service.binder,
        bind_owner=thread_initial_execution_service.bind_owner,
    )
    dependency_provider.set_session_subagent_service(session_subagent_service)
    team_fanout_service = TeamFanoutService(
        factory=thread_creation_factory, sessions=session_service, saver=checkpointer,
        dispatcher=runtime_source_dispatcher, publication_lock=mcp_catalog_owner.publication_lock,
    )
    team_service = TeamCoordinationService(
        store=TeamStore(factory=thread_creation_factory, write_admission=session_service.session_write_admission),
        session_service=session_service,
        fanout=team_fanout_service,
        session_subagent_service=session_subagent_service,
        thread_creation_factory=thread_creation_factory,
    )
    team_fanout_service.bind_wakeup_finalizer(team_service.finalize_wakeup)
    dependency_provider.set_team_service(team_service)
    session_turn_replay_service = SessionTurnReplayService(
        attachment_holder=attachment_blob_store.deletion_holder,
        checkpointer=checkpointer,
        message_service=message_service,
        session_service=session_service,
        job_service=job_service,
        dispatcher=session_orchestrator,
        trace_event_store=trace_event_store,
    )

    agent_service = AgentService(config_service=config_service)
    artifact_service = ArtifactService()
    event_service = EventService(bus=job_event_bus)
    runtime_service = RuntimeService(
        workspace_id=workspace_service.workspace_id,
        workspace_root=resolved_workspace_root,
        job_service=job_service,
        background_task_registry=background_task_registry,
        trace_event_store=trace_event_store,
        message_stream_store=message_stream_store,
        terminal_status_writer=checkpointer,
        pending_tool_checkpoint_id=agent_execution_service.pending_tool_checkpoint_id,
        pending_request_store=pending_request_store,
    )
    goal_runtime_service = GoalRuntimeService(
        goal_service=goal_service,
        job_service=job_service,
        session_orchestrator=session_orchestrator,
    )
    session_interrupt_service = SessionInterruptService(
        session_service=session_service,
        job_service=job_service,
        job_event_bus=job_event_bus,
        message_service=message_service,
        message_stream_store=message_stream_store,
        execution_scope_registry=agent_execution_service.execution_scope_registry,
    )
    session_skill_tracking_service = SessionSkillTrackingService(
        checkpointer=checkpointer,
        session_service=session_service,
    )
    session_resource_mapper = SessionResourceMapper()
    session_resource_provider_registry = SessionResourceProviderRegistry(
        [
            BackgroundTaskResourceProvider(
                task_registry=background_task_registry,
                resource_mapper=session_resource_mapper,
            ),
            TerminalResourceProvider(
                terminal_owner=terminal_resource_owner,
                resource_mapper=session_resource_mapper,
            ),
            BrowserResourceProvider(
                browser_owner=browser_resource_owner,
                resource_mapper=session_resource_mapper,
            ),
        ]
    )
    session_resource_service = SessionResourceService(
        session_service=session_service,
        job_service=job_service,
        provider_registry=session_resource_provider_registry,
    )
    if isinstance(session_path_resolver, SessionCatalogPathResolver):
        # 子树删除的单一 drain 回调按固定顺序收敛所有运行时 owner：先
        # NodeDebug，再清理终端、浏览器和后台任务资源。该回调在每个
        # session 的生命周期 gate 内执行，并且先于 fence、物理隔离和
        # drain 进度记录，保证部分失败可按 record 定点重试。
        async def drain_session_resources(session_id: str) -> None:
            session_service.settle_abandoned_context_controls(session_id)
            await node_debug_service.drain_session(session_id)
            await session_resource_service.cleanup_session(session_id)
            await session_communication_service.observations.drain_session(session_id)
            await team_fanout_service.drain_session(session_id)
            # 所有领域资源排空后关闭本 Session 的真实 graph scope；旧
            # residency 记账不能越过 fence/rename 后再查询已删除控制库。
            await thread_residency_tracker.close(session_id=session_id)
            background_message_bus.close_session(session_id)
            # 附件引用释放与 ingest/claim 定点收敛必须先于 fence CAS 与物理
            # 隔离：删除 owner 按 pin 阻断 canonical 使用、释放 reference 后
            # 才隔离 Session 节点，且不留下指向已删除 owner 的 reference。
            attachment_blob_store.release_session_references(session_id)
            collected = await attachment_blob_store.collect_garbage(leases=external_resource_leases)
            await observe_phase(session_id, "attachments.gc.completed", details={
                "collected_attachment_ids": list(collected),
            })

        session_path_resolver.bind_fork_retention_release_callback(checkpointer.release_fork_retentions)
        session_path_resolver.bind_fork_child_abort_callback(checkpointer.forks.abort_prepared_child_fork)
        session_path_resolver.bind_session_drain_callback(
            drain_session_resources, holders=(session_communication_service.deletion_holder, team_fanout_service.deletion_holder,
                                             attachment_blob_store.deletion_holder,
                                             terminal_resource_owner.deletion_holder),
        )
    session_information_service = SessionInformationService(
        session_service=session_service,
        session_resource_service=session_resource_service,
        workspace_service=workspace_service,
    )
    session_context_query_service.bind_information_source(
        session_information_service
    )
    context_checkpoint_store = ContextCompactionCheckpointStore(
        checkpointer=checkpointer,
    )
    summarization_compactor = AgentSummarizationCompactor(
        config_service=config_service,
        history_backend=build_workspace_backend(resolved_workspace_root),
    )
    context_compaction_service = ContextCompactionService(
        checkpoint_store=context_checkpoint_store,
        job_service=job_service,
        session_service=session_service,
        summarization_compactor=summarization_compactor,
    )
    llm_request_log_service = LLMRequestLogService(
        resolved_sessions_root,
        path_resolver=session_path_resolver,
    )
    log_service = LogService(workspace_root=resolved_workspace_root, path_resolver=session_path_resolver)
    tool_test_service = ToolTestService(
        config_service=config_service,
        registry=ToolTestRegistry(),
        store=ToolTestStore(root=resolved_boxteam_root / "tool_tests"),
        workspace_root=resolved_workspace_root,
        asset_root=resolved_project_root
        / "tests"
        / "fixtures"
        / "workspaces"
        / "model_tool_test_workspace",
    )
    tool_catalog_service = ToolCatalogService(
        runtime_catalog=agent_execution_service,
        config_service=config_service,
    )
    tool_service = ToolService(
        tool_catalog=tool_catalog_service,
        selection_store=tool_selection_store,
        config_service=config_service,
        test_supported_tools=tool_test_service.supported_tools,
    )
    return AppContainer(
        workspace_id=workspace_id,
        session_catalog=session_catalog,
        workspace_root=resolved_workspace_root,
        config_service=config_service,
        workspace_source_owner=workspace_source_owner,
        agent_service=agent_service,
        artifact_service=artifact_service,
        event_service=event_service,
        job_service=job_service,
        message_service=message_service,
        attachment_blob_store=attachment_blob_store,
        external_resource_leases=external_resource_leases,
        runtime_service=runtime_service,
        goal_service=goal_service,
        goal_runtime_service=goal_runtime_service,
        session_interrupt_service=session_interrupt_service,
        session_skill_tracking_service=session_skill_tracking_service,
        session_context_fork_service=session_context_fork_service,
        session_turn_replay_service=session_turn_replay_service,
        session_turn_history_service=session_turn_history_service,
        session_changes_service=session_changes_service,
        session_information_service=session_information_service,
        session_context_query_service=session_context_query_service,
        workspace_session_context_transport=workspace_session_context_transport,
        session_resource_service=session_resource_service,
        context_compaction_service=context_compaction_service,
        terminal_resource_owner=terminal_resource_owner,
        browser_resource_owner=browser_resource_owner,
        session_service=session_service,
        session_orchestrator=session_orchestrator,
        session_communication_service=session_communication_service,
        runtime_source_dispatcher=runtime_source_dispatcher,
        thread_initial_execution_service=thread_initial_execution_service,
        terminal_steering_service=terminal_steering_service,
        session_catalog_service=session_catalog_service,
        session_generation_service=session_generation_service,
        session_subagent_service=session_subagent_service,
        team_service=team_service,
        team_fanout_service=team_fanout_service,
        llm_request_log_service=llm_request_log_service,
        log_service=log_service,
        tool_service=tool_service,
        tool_test_service=tool_test_service,
        tool_selection_store=tool_selection_store,
        workspace_service=workspace_service,
        workspace_file_watch_service=workspace_file_watch_service,
        workspace_file_resource_registry=workspace_file_resource_registry,
        resource_platform=resource_platform,
        resource_navigation=resource_navigation,
        text_mutations=text_mutations,
        file_tree_settings_service=file_tree_settings_service,
        agent_execution_service=agent_execution_service,
        node_debug_service=node_debug_service,
        job_event_bus=job_event_bus,
        thread_residency_tracker=thread_residency_tracker,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        trace_event_store=trace_event_store,
        trace_event_recorder=trace_event_recorder,
        rollout_checkpoint_runtime=rollout_checkpoint_runtime,
        checkpointer=checkpointer,
        mcp_catalog_owner=mcp_catalog_owner,
        pending_request_store=pending_request_store,
        mcp_connection_service=mcp_connection_service,
        workspace_activity_service=workspace_activity_service,
        message_stream_store=message_stream_store,
    )


def build_app_container(
    *,
    project_root: str | Path | None = None,
    workspace_root: str | Path,
    startup_mount: MountedWorkspace | None = None,
    clock_ports: RuntimeClockPorts = DEFAULT_RUNTIME_CLOCK_PORTS,
) -> AppContainer:
    """在显式工作区上下文中装配唯一完整服务图。"""

    resolved_root = Path(workspace_root).expanduser().resolve(strict=True)
    with AttachmentMaintenanceLock(
        resolved_root / ".boxteam" / ATTACHMENT_ROOT_DIRECTORY_NAME, exclusive=False,
    ), ExitStack() as construction:
        workspace_id = load_or_create_workspace_id(resolved_root)
        with workspace_context(workspace_id, resolved_root):
            container = _build_app_container(
                project_root=project_root,
                workspace_root=resolved_root,
                startup_mount=startup_mount,
                clock_ports=clock_ports,
                construction=construction,
            )
            # 未发布服务图的持久owner由构造作用域回收；成功后交给原WorkspaceLifespan。
            construction.pop_all()
            return container
