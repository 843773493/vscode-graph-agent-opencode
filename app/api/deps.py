from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import JobEventBus
from app.services.orchestration.agent_execution_service import AgentExecutionService
from app.services.business.agent_service import AgentService
from app.services.business.context_compaction_service import ContextCompactionService
from app.services.infrastructure.artifact_service import ArtifactService
from app.services.infrastructure.config_service import ConfigService
from app.services.event_service import EventService
from app.services.business.job_service import JobService
from app.services.business.message_service import MessageService
from app.services.business.session_resource_service import SessionResourceService
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.orchestration.session_auto_continue_service import SessionAutoContinueService
from app.services.business.session_interrupt_service import SessionInterruptService
from app.services.business.session_service import SessionService
from app.services.infrastructure.llm_request_log_service import LLMRequestLogService
from app.services.infrastructure.log_service import LogService
from app.services.infrastructure.tool_service import ToolService
from app.services.infrastructure.workspace_service import WorkspaceService
from app.runtime.session_orchestrator import SessionOrchestrator


class _AppContainerProtocol:
    config_service: ConfigService
    agent_service: AgentService
    artifact_service: ArtifactService
    background_message_bus: BackgroundMessageBus
    background_task_registry: BackgroundTaskRegistry
    event_service: EventService
    job_event_bus: JobEventBusProtocol
    job_service: JobServiceProtocol
    message_service: MessageService
    runtime_service: RuntimeService
    session_auto_continue_service: SessionAutoContinueService
    session_interrupt_service: SessionInterruptService
    context_compaction_service: ContextCompactionService
    session_resource_service: SessionResourceService
    session_service: SessionService
    llm_request_log_service: LLMRequestLogService
    log_service: LogService
    tool_service: ToolService
    workspace_service: WorkspaceService
    agent_execution_service: AgentExecutionService
    session_orchestrator: SessionOrchestrator


def get_request_id(x_request_id: str | None = Header(default=None)) -> str | None:
    return x_request_id


def verify_local_token(x_local_token: str | None = Header(default=None)) -> str:
    expected = "local-dev-token"
    if x_local_token != expected:
        raise HTTPException(status_code=401, detail="invalid local token")
    return x_local_token


def get_config_service(request: Request) -> ConfigService:
    container = getattr(request.app.state, "container", None)
    config_service = getattr(container, "config_service", None) if container is not None else None
    if not isinstance(config_service, ConfigService):
        raise RuntimeError("ConfigService 尚未在应用启动阶段初始化")
    return config_service


def _get_container(request: Request) -> _AppContainerProtocol:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("应用容器尚未初始化")
    return container


def get_agent_service(request: Request) -> AgentService:
    service = getattr(_get_container(request), "agent_service", None)
    if not isinstance(service, AgentService):
        raise RuntimeError("AgentService 尚未在应用启动阶段初始化")
    return service


def get_artifact_service(request: Request) -> ArtifactService:
    service = getattr(_get_container(request), "artifact_service", None)
    if not isinstance(service, ArtifactService):
        raise RuntimeError("ArtifactService 尚未在应用启动阶段初始化")
    return service


def get_background_message_bus(request: Request) -> BackgroundMessageBus:
    service = getattr(_get_container(request), "background_message_bus", None)
    if not isinstance(service, BackgroundMessageBus):
        raise RuntimeError("BackgroundMessageBus 尚未在应用启动阶段初始化")
    return service


def get_background_task_registry(request: Request) -> BackgroundTaskRegistry:
    service = getattr(_get_container(request), "background_task_registry", None)
    if not isinstance(service, BackgroundTaskRegistry):
        raise RuntimeError("BackgroundTaskRegistry 尚未在应用启动阶段初始化")
    return service


def get_event_service(request: Request) -> EventService:
    service = getattr(_get_container(request), "event_service", None)
    if not isinstance(service, EventService):
        raise RuntimeError("EventService 尚未在应用启动阶段初始化")
    return service


def get_job_event_bus(request: Request) -> JobEventBusProtocol:
    service = getattr(_get_container(request), "job_event_bus", None)
    if not isinstance(service, JobEventBusProtocol):
        raise RuntimeError("JobEventBus 尚未在应用启动阶段初始化")
    return service


def get_job_service(request: Request) -> JobServiceProtocol:
    service = getattr(_get_container(request), "job_service", None)
    if not isinstance(service, JobServiceProtocol):
        raise RuntimeError("JobService 尚未在应用启动阶段初始化")
    return service


def get_message_service(request: Request) -> MessageService:
    service = getattr(_get_container(request), "message_service", None)
    if not isinstance(service, MessageService):
        raise RuntimeError("MessageService 尚未在应用启动阶段初始化")
    return service


def get_runtime_service(request: Request) -> RuntimeService:
    service = getattr(_get_container(request), "runtime_service", None)
    if not isinstance(service, RuntimeService):
        raise RuntimeError("RuntimeService 尚未在应用启动阶段初始化")
    return service


def get_session_auto_continue_service(request: Request) -> SessionAutoContinueService:
    service = getattr(_get_container(request), "session_auto_continue_service", None)
    if not isinstance(service, SessionAutoContinueService):
        raise RuntimeError("SessionAutoContinueService 尚未在应用启动阶段初始化")
    return service


def get_session_interrupt_service(request: Request) -> SessionInterruptService:
    service = getattr(_get_container(request), "session_interrupt_service", None)
    if not isinstance(service, SessionInterruptService):
        raise RuntimeError("SessionInterruptService 尚未在应用启动阶段初始化")
    return service


def get_context_compaction_service(request: Request) -> ContextCompactionService:
    service = getattr(_get_container(request), "context_compaction_service", None)
    if not isinstance(service, ContextCompactionService):
        raise RuntimeError("ContextCompactionService 尚未在应用启动阶段初始化")
    return service


def get_session_resource_service(request: Request) -> SessionResourceService:
    service = getattr(_get_container(request), "session_resource_service", None)
    if not isinstance(service, SessionResourceService):
        raise RuntimeError("SessionResourceService 尚未在应用启动阶段初始化")
    return service


def get_session_service(request: Request) -> SessionService:
    service = getattr(_get_container(request), "session_service", None)
    if not isinstance(service, SessionService):
        raise RuntimeError("SessionService 尚未在应用启动阶段初始化")
    return service


def get_llm_request_log_service(request: Request) -> LLMRequestLogService:
    service = getattr(_get_container(request), "llm_request_log_service", None)
    if not isinstance(service, LLMRequestLogService):
        raise RuntimeError("LLMRequestLogService 尚未在应用启动阶段初始化")
    return service


def get_log_service(request: Request) -> LogService:
    service = getattr(_get_container(request), "log_service", None)
    if not isinstance(service, LogService):
        raise RuntimeError("LogService 尚未在应用启动阶段初始化")
    return service


def get_tool_service(request: Request) -> ToolService:
    service = getattr(_get_container(request), "tool_service", None)
    if not isinstance(service, ToolService):
        raise RuntimeError("ToolService 尚未在应用启动阶段初始化")
    return service


def get_workspace_service(request: Request) -> WorkspaceService:
    service = getattr(_get_container(request), "workspace_service", None)
    if not isinstance(service, WorkspaceService):
        raise RuntimeError("WorkspaceService 尚未在应用启动阶段初始化")
    return service


def get_agent_execution_service(request: Request) -> AgentExecutionService:
    service = getattr(_get_container(request), "agent_execution_service", None)
    if not isinstance(service, AgentExecutionService):
        raise RuntimeError("AgentExecutionService 尚未在应用启动阶段初始化")
    return service


def get_session_orchestrator(request: Request) -> SessionOrchestrator:
    service = getattr(_get_container(request), "session_orchestrator", None)
    if not isinstance(service, SessionOrchestrator):
        raise RuntimeError("SessionOrchestrator 尚未在应用启动阶段初始化")
    return service
