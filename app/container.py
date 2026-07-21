from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.agents.context_checkpoint_store import ContextCompactionCheckpointStore
from app.agents.context_compaction_adapter import AgentSummarizationCompactor
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_subagent import SessionSubagentProtocol
from app.abstractions.team import TeamCoordinationProtocol
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.core.env import get_project_root
from app.core.job_event_bus import JobEventBus
from app.core.path_utils import (
    get_sessions_dir,
    get_workspace_root,
)
from app.runtime.agent_runtime import AgentRuntimeDependencyProvider
from app.runtime.session_orchestrator import SessionOrchestrator
from app.services.business.agent_service import AgentService
from app.services.business.context_compaction_service import ContextCompactionService
from app.services.business.job.service import JobService
from app.services.business.message_service import MessageService
from app.services.business.session_interrupt_service import SessionInterruptService
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_turn_replay_service import SessionTurnReplayService
from app.services.business.session_changes_service import SessionChangesService
from app.services.business.session_information_service import SessionInformationService
from app.services.business.session_context_query_service import SessionContextQueryService
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
from app.services.event_service import EventService
from app.services.infrastructure.artifact_service import ArtifactService
from app.services.infrastructure.background_task_history_store import (
    BackgroundTaskHistoryStore,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.context_history_store import ContextHistoryStore
from app.services.infrastructure.historical_terminal_record_reader import (
    HistoricalTerminalRecordReader,
)
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from app.services.infrastructure.llm_request_log_service import LLMRequestLogService
from app.services.infrastructure.log_service import LogService
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.infrastructure.session_changes_store import SessionChangesStore
from app.services.infrastructure.session_attachment_store import SessionAttachmentStore
from app.services.infrastructure.tool_service import ToolService
from app.services.infrastructure.tool_catalog_service import ToolCatalogService
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from app.services.infrastructure.pending_request_store import PendingRequestStore
from app.services.infrastructure.mcp import McpRuntimeManager
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.infrastructure.trace_event_recorder import TraceEventRecorder
from app.services.infrastructure.workspace_service import WorkspaceService
from app.services.mapping.session_resource_mapper import SessionResourceMapper
from app.services.orchestration.agent_execution_service import AgentExecutionService
from app.services.orchestration.job_execution_service import JobExecutionService
from app.services.orchestration.session_auto_continue_service import (
    SessionAutoContinueService,
)
from app.services.orchestration.session_title_service import SessionTitleService
from app.services.orchestration.session_subagent_service import SessionSubagentService
from app.services.business.team.service import TeamCoordinationService
from app.services.infrastructure.team.store import TeamStore
from app.tool_testing import ToolTestRegistry, ToolTestService, ToolTestStore


class _AgentRuntimeDependencyProvider(AgentRuntimeDependencyProvider):
    def __init__(
        self,
        *,
        message_service: MessageService,
        session_service: SessionService,
        checkpointer: BaseCheckpointSaver,
        terminal_manager_client: TerminalManagerClient,
        browser_manager_client: BrowserManagerClient,
        session_context_query_service: SessionContextQueryService,
        workspace_session_context_client: GatewaySessionContextClient,
        mcp_runtime_manager: McpRuntimeManager,
    ) -> None:
        self._message_service = message_service
        self._session_service = session_service
        self._checkpointer = checkpointer
        self._terminal_manager_client = terminal_manager_client
        self._browser_manager_client = browser_manager_client
        self._session_context_query_service = session_context_query_service
        self._workspace_session_context_client = workspace_session_context_client
        self._mcp_runtime_manager = mcp_runtime_manager
        self._job_service: JobServiceProtocol | None = None
        self._session_orchestrator: SessionOrchestrator | None = None
        self._session_subagent_service: SessionSubagentProtocol | None = None
        self._team_service: TeamCoordinationProtocol | None = None

    def get_message_service(self) -> MessageService:
        return self._message_service

    def get_session_service(self) -> SessionService:
        return self._session_service

    def get_checkpointer(self) -> BaseCheckpointSaver:
        return self._checkpointer

    def get_terminal_manager_client(self) -> TerminalManagerClient:
        return self._terminal_manager_client

    def get_browser_manager_client(self) -> BrowserManagerClient:
        return self._browser_manager_client

    def get_session_context_query_service(self) -> SessionContextQueryService:
        return self._session_context_query_service

    def get_workspace_session_context_client(self) -> GatewaySessionContextClient:
        return self._workspace_session_context_client

    def get_mcp_tools(self) -> list[BaseTool]:
        return self._mcp_runtime_manager.get_tools()

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
    config_service: ConfigService
    agent_service: AgentService
    artifact_service: ArtifactService
    event_service: EventService
    job_service: JobServiceProtocol
    message_service: MessageService
    session_attachment_store: SessionAttachmentStore
    runtime_service: RuntimeService
    session_auto_continue_service: SessionAutoContinueService
    session_interrupt_service: SessionInterruptService
    session_context_fork_service: SessionContextForkService
    session_turn_replay_service: SessionTurnReplayService
    session_changes_service: SessionChangesService
    session_information_service: SessionInformationService
    session_context_query_service: SessionContextQueryService
    session_resource_service: SessionResourceService
    context_compaction_service: ContextCompactionService
    session_service: SessionService
    session_orchestrator: SessionOrchestrator
    session_subagent_service: SessionSubagentService
    team_service: TeamCoordinationService
    llm_request_log_service: LLMRequestLogService
    log_service: LogService
    tool_service: ToolService
    tool_test_service: ToolTestService
    tool_selection_store: ToolSelectionStore
    workspace_service: WorkspaceService
    agent_execution_service: AgentExecutionService
    job_event_bus: JobEventBusProtocol
    background_task_registry: BackgroundTaskRegistry
    background_message_bus: BackgroundMessageBus
    trace_event_store: TraceEventStore
    trace_event_recorder: TraceEventRecorder
    mcp_runtime_manager: McpRuntimeManager


def build_app_container(
    *,
    project_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
) -> AppContainer:
    resolved_project_root = get_project_root(project_root)
    resolved_workspace_root = (
        Path(workspace_root).resolve() if workspace_root is not None else get_workspace_root()
    )
    resolved_boxteam_root = resolved_workspace_root / ".boxteam"
    job_event_bus = JobEventBus()
    background_task_registry = BackgroundTaskRegistry(
        history_store=BackgroundTaskHistoryStore(sessions_dir=get_sessions_dir())
    )
    background_message_bus = BackgroundMessageBus()
    trace_event_store = TraceEventStore(sessions_dir=get_sessions_dir())
    trace_event_recorder = TraceEventRecorder(bus=job_event_bus, store=trace_event_store)
    terminal_manager_client = TerminalManagerClient()
    browser_manager_client = BrowserManagerClient()
    workspace_session_context_client = GatewaySessionContextClient()

    checkpointer = FileSystemCheckpointSaver(sessions_dir=get_sessions_dir())

    config_service = ConfigService(
        workspace_root=resolved_workspace_root,
    )
    mcp_runtime_manager = McpRuntimeManager(
        raw_config=config_service.get_mcp_config(),
        workspace_root=resolved_workspace_root,
    )
    session_attachment_store = SessionAttachmentStore(resolved_workspace_root)
    message_service = MessageService(
        checkpointer=checkpointer,
        attachment_store=session_attachment_store,
    )
    session_service = SessionService(config_service=config_service, trace_event_store=trace_event_store)
    session_context_query_service = SessionContextQueryService(
        message_source=message_service,
        session_lookup=session_service,
    )
    session_context_fork_service = SessionContextForkService(
        session_service=session_service,
        checkpointer=checkpointer,
    )
    session_changes_store = SessionChangesStore(workspace_root=resolved_workspace_root)
    session_changes_service = SessionChangesService(
        session_service=session_service,
        store=session_changes_store,
    )
    tool_selection_store = ToolSelectionStore(boxteam_root=resolved_boxteam_root)
    dependency_provider = _AgentRuntimeDependencyProvider(
        message_service=message_service,
        session_service=session_service,
        checkpointer=checkpointer,
        terminal_manager_client=terminal_manager_client,
        browser_manager_client=browser_manager_client,
        session_context_query_service=session_context_query_service,
        workspace_session_context_client=workspace_session_context_client,
        mcp_runtime_manager=mcp_runtime_manager,
    )
    agent_execution_service = AgentExecutionService(
        config_service=config_service,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        dependency_provider=dependency_provider,
        session_changes_service=session_changes_service,
        tool_selection_store=tool_selection_store,
        workspace_root=resolved_workspace_root,
    )
    session_title_service = SessionTitleService(
        session_service=session_service,
        job_event_bus=job_event_bus,
    )
    job_executor = JobExecutionService(
        agent_execution_service=agent_execution_service,
        message_service=message_service,
        job_event_bus=job_event_bus,
        session_title_service=session_title_service,
    )
    job_service = JobService(
        job_event_bus=job_event_bus,
        job_executor=job_executor,
        pending_request_store=PendingRequestStore(sessions_dir=get_sessions_dir()),
    )
    dependency_provider.set_job_service(job_service)
    session_orchestrator = SessionOrchestrator(
        message_service=message_service,
        session_service=session_service,
        config_service=config_service,
        job_service=job_service,
        job_event_bus=job_event_bus,
    )
    dependency_provider.set_session_orchestrator(session_orchestrator)
    session_subagent_service = SessionSubagentService(
        session_service=session_service,
        session_orchestrator=session_orchestrator,
    )
    dependency_provider.set_session_subagent_service(session_subagent_service)
    team_service = TeamCoordinationService(
        store=TeamStore(workspace_root=resolved_workspace_root),
        session_service=session_service,
        session_orchestrator=session_orchestrator,
        session_subagent_service=session_subagent_service,
    )
    dependency_provider.set_team_service(team_service)
    session_turn_replay_service = SessionTurnReplayService(
        checkpointer=checkpointer,
        message_service=message_service,
        session_service=session_service,
        job_service=job_service,
        dispatcher=session_orchestrator,
    )

    agent_service = AgentService(config_service=config_service)
    artifact_service = ArtifactService()
    event_service = EventService(bus=job_event_bus)
    runtime_service = RuntimeService(
        job_service=job_service,
        background_task_registry=background_task_registry,
        trace_event_store=trace_event_store,
    )
    session_auto_continue_service = SessionAutoContinueService(
        background_task_registry=background_task_registry,
        job_event_bus=job_event_bus,
        session_service=session_service,
        job_service=job_service,
        session_orchestrator=session_orchestrator,
    )
    session_interrupt_service = SessionInterruptService(
        job_service=job_service,
        job_event_bus=job_event_bus,
        message_service=message_service,
    )
    historical_terminal_reader = HistoricalTerminalRecordReader(
        sessions_dir=get_sessions_dir(),
    )
    session_resource_mapper = SessionResourceMapper()
    session_resource_provider_registry = SessionResourceProviderRegistry(
        [
            BackgroundTaskResourceProvider(
                task_registry=background_task_registry,
                message_service=message_service,
                resource_mapper=session_resource_mapper,
            ),
            TerminalResourceProvider(
                terminal_manager=terminal_manager_client,
                historical_reader=historical_terminal_reader,
                message_service=message_service,
                resource_mapper=session_resource_mapper,
            ),
            BrowserResourceProvider(
                browser_manager=browser_manager_client,
                resource_mapper=session_resource_mapper,
            ),
        ]
    )
    session_resource_service = SessionResourceService(
        session_service=session_service,
        job_service=job_service,
        provider_registry=session_resource_provider_registry,
    )
    workspace_service = WorkspaceService(config_service=config_service)
    session_information_service = SessionInformationService(
        session_service=session_service,
        session_resource_service=session_resource_service,
        workspace_service=workspace_service,
    )
    context_history_store = ContextHistoryStore()
    context_checkpoint_store = ContextCompactionCheckpointStore(
        checkpointer=checkpointer,
    )
    summarization_compactor = AgentSummarizationCompactor(
        config_service=config_service,
        history_store=context_history_store,
    )
    context_compaction_service = ContextCompactionService(
        checkpoint_store=context_checkpoint_store,
        job_service=job_service,
        session_service=session_service,
        summarization_compactor=summarization_compactor,
    )
    llm_request_log_service = LLMRequestLogService()
    log_service = LogService()
    tool_test_service = ToolTestService(
        config_service=config_service,
        registry=ToolTestRegistry(),
        store=ToolTestStore(root=resolved_boxteam_root / "tool_tests"),
        workspace_root=resolved_workspace_root,
        asset_root=resolved_project_root / "asset" / "model_tool_test_workspace",
    )
    tool_catalog_service = ToolCatalogService(
        runtime_catalog=agent_execution_service,
        config_service=config_service,
    )
    tool_service = ToolService(
        tool_catalog=tool_catalog_service,
        selection_store=tool_selection_store,
        test_supported_tools=tool_test_service.supported_tools,
    )
    return AppContainer(
        config_service=config_service,
        agent_service=agent_service,
        artifact_service=artifact_service,
        event_service=event_service,
        job_service=job_service,
        message_service=message_service,
        session_attachment_store=session_attachment_store,
        runtime_service=runtime_service,
        session_auto_continue_service=session_auto_continue_service,
        session_interrupt_service=session_interrupt_service,
        session_context_fork_service=session_context_fork_service,
        session_turn_replay_service=session_turn_replay_service,
        session_changes_service=session_changes_service,
        session_information_service=session_information_service,
        session_context_query_service=session_context_query_service,
        session_resource_service=session_resource_service,
        context_compaction_service=context_compaction_service,
        session_service=session_service,
        session_orchestrator=session_orchestrator,
        session_subagent_service=session_subagent_service,
        team_service=team_service,
        llm_request_log_service=llm_request_log_service,
        log_service=log_service,
        tool_service=tool_service,
        tool_test_service=tool_test_service,
        tool_selection_store=tool_selection_store,
        workspace_service=workspace_service,
        agent_execution_service=agent_execution_service,
        job_event_bus=job_event_bus,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        trace_event_store=trace_event_store,
        trace_event_recorder=trace_event_recorder,
        mcp_runtime_manager=mcp_runtime_manager,
    )
