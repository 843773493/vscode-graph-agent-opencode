from __future__ import annotations

from dataclasses import dataclass

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import JobEventBus
from app.services.orchestration.agent_execution_service import AgentExecutionService
from app.services.business.agent_service import AgentService
from app.services.infrastructure.artifact_service import ArtifactService
from app.services.infrastructure.config_service import ConfigService
from app.services.event_service import EventService
from app.services.business.job_service import JobService
from app.services.orchestration.job_execution_service import JobExecutionService
from app.services.business.message_service import MessageService
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.orchestration.session_auto_continue_service import SessionAutoContinueService
from app.services.business.session_service import SessionService
from app.services.infrastructure.log_service import LogService
from app.services.infrastructure.tool_service import ToolService
from app.services.infrastructure.workspace_service import WorkspaceService
from app.runtime.session_orchestrator import SessionOrchestrator
from app.runtime.agent_runtime import AgentRuntimeDependencyProvider

from app.core.path_utils import get_logs_dir
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.orchestration.trace_event_recorder import TraceEventRecorder


class _AgentRuntimeDependencyProvider(AgentRuntimeDependencyProvider):
    def __init__(self, *, message_service: MessageService, session_service: SessionService) -> None:
        self._message_service = message_service
        self._session_service = session_service
        self._session_orchestrator: SessionOrchestrator | None = None

    def get_message_service(self) -> MessageService:
        return self._message_service

    def get_session_service(self) -> SessionService:
        return self._session_service

    def set_session_orchestrator(self, session_orchestrator: SessionOrchestrator) -> None:
        self._session_orchestrator = session_orchestrator

    def get_session_orchestrator(self) -> SessionOrchestrator:
        if self._session_orchestrator is None:
            raise RuntimeError("_AgentRuntimeDependencyProvider 未绑定 SessionOrchestrator")
        return self._session_orchestrator


@dataclass(slots=True)
class AppContainer:
    config_service: ConfigService
    agent_service: AgentService
    artifact_service: ArtifactService
    event_service: EventService
    job_service: JobServiceProtocol
    message_service: MessageService
    runtime_service: RuntimeService
    session_auto_continue_service: SessionAutoContinueService
    session_service: SessionService
    session_orchestrator: SessionOrchestrator
    log_service: LogService
    tool_service: ToolService
    workspace_service: WorkspaceService
    agent_execution_service: AgentExecutionService
    job_event_bus: JobEventBusProtocol
    background_task_registry: BackgroundTaskRegistry
    background_message_bus: BackgroundMessageBus
    trace_event_store: TraceEventStore
    trace_event_recorder: TraceEventRecorder


def build_app_container() -> AppContainer:
    job_event_bus = JobEventBus()
    background_task_registry = BackgroundTaskRegistry()
    background_message_bus = BackgroundMessageBus()
    trace_event_store = TraceEventStore(logs_dir=get_logs_dir())
    trace_event_recorder = TraceEventRecorder(bus=job_event_bus, store=trace_event_store)

    config_service = ConfigService()
    message_service = MessageService()
    session_service = SessionService(config_service=config_service, trace_event_store=trace_event_store)
    dependency_provider = _AgentRuntimeDependencyProvider(
        message_service=message_service,
        session_service=session_service,
    )
    agent_execution_service = AgentExecutionService(
        config_service=config_service,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        dependency_provider=dependency_provider,
    )
    job_executor = JobExecutionService(
        agent_execution_service=agent_execution_service,
        message_service=message_service,
        job_event_bus=job_event_bus,
    )
    job_service = JobService(job_event_bus=job_event_bus, job_executor=job_executor)
    session_orchestrator = SessionOrchestrator(
        message_service=message_service,
        session_service=session_service,
        config_service=config_service,
        job_service=job_service,
        job_event_bus=job_event_bus,
    )
    dependency_provider.set_session_orchestrator(session_orchestrator)

    agent_service = AgentService(config_service=config_service)
    artifact_service = ArtifactService()
    event_service = EventService(bus=job_event_bus)
    runtime_service = RuntimeService(job_service=job_service)
    session_auto_continue_service = SessionAutoContinueService(
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        session_service=session_service,
        job_service=job_service,
        session_orchestrator=session_orchestrator,
    )
    log_service = LogService()
    tool_service = ToolService(tool_catalog=agent_execution_service)
    workspace_service = WorkspaceService(config_service=config_service)

    return AppContainer(
        config_service=config_service,
        agent_service=agent_service,
        artifact_service=artifact_service,
        event_service=event_service,
        job_service=job_service,
        message_service=message_service,
        runtime_service=runtime_service,
        session_auto_continue_service=session_auto_continue_service,
        session_service=session_service,
        session_orchestrator=session_orchestrator,
        log_service=log_service,
        tool_service=tool_service,
        workspace_service=workspace_service,
        agent_execution_service=agent_execution_service,
        job_event_bus=job_event_bus,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        trace_event_store=trace_event_store,
        trace_event_recorder=trace_event_recorder,
    )
