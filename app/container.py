from __future__ import annotations

from dataclasses import dataclass

from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import JobEventBus
from app.services.agent_execution_service import AgentExecutionService
from app.services.agent_service import AgentService
from app.services.artifact_service import ArtifactService
from app.services.config_service import ConfigService
from app.services.event_service import EventService
from app.services.job_service import JobService
from app.services.job_execution_service import JobExecutionService
from app.services.message_service import MessageService
from app.services.runtime_service import RuntimeService
from app.services.session_auto_continue_service import SessionAutoContinueService
from app.services.session_service import SessionService
from app.services.log_service import LogService
from app.services.tool_service import ToolService
from app.services.workspace_service import WorkspaceService
from app.agents.agent_middleware import LLMLoggingMiddleware


@dataclass(slots=True)
class AppContainer:
    config_service: ConfigService
    agent_service: AgentService
    artifact_service: ArtifactService
    event_service: EventService
    job_service: JobService
    message_service: MessageService
    runtime_service: RuntimeService
    session_auto_continue_service: SessionAutoContinueService
    session_service: SessionService
    log_service: LogService
    tool_service: ToolService
    workspace_service: WorkspaceService
    agent_execution_service: AgentExecutionService
    llm_logging_middleware: LLMLoggingMiddleware
    job_event_bus: JobEventBus
    background_task_registry: BackgroundTaskRegistry
    background_message_bus: BackgroundMessageBus


def build_app_container() -> AppContainer:
    job_event_bus = JobEventBus()
    background_task_registry = BackgroundTaskRegistry()
    background_message_bus = BackgroundMessageBus()

    config_service = ConfigService()
    message_service = MessageService()
    session_service = SessionService(config_service=config_service)
    agent_execution_service = AgentExecutionService(
        config_service=config_service,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        message_service=message_service,
        session_service=session_service,
    )
    job_executor = JobExecutionService(
        agent_execution_service=agent_execution_service,
        message_service=message_service,
        job_event_bus=job_event_bus,
    )
    job_service = JobService(job_event_bus=job_event_bus, job_executor=job_executor)

    agent_service = AgentService(config_service=config_service)
    artifact_service = ArtifactService()
    event_service = EventService(bus=job_event_bus)
    runtime_service = RuntimeService(job_service=job_service)
    session_auto_continue_service = SessionAutoContinueService(
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        session_service=session_service,
        message_service=message_service,
        job_service=job_service,
        config_service=config_service,
    )
    log_service = LogService()
    tool_service = ToolService(agent_execution_service=agent_execution_service)
    workspace_service = WorkspaceService(config_service=config_service)
    llm_logging_middleware = LLMLoggingMiddleware(job_event_bus=job_event_bus)

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
        log_service=log_service,
        tool_service=tool_service,
        workspace_service=workspace_service,
        agent_execution_service=agent_execution_service,
        llm_logging_middleware=llm_logging_middleware,
        job_event_bus=job_event_bus,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
    )