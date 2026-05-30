"""
应用级依赖提供者和服务容器。

这个模块只负责：
1. 在应用启动后缓存一次服务实例
2. 向 FastAPI 依赖注入提供统一入口

注意：这里不再表达“全局单例”语义，服务实例由应用生命周期管理。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.agent_service import AgentService
    from app.services.artifact_service import ArtifactService
    from app.services.config_service import ConfigService
    from app.services.event_service import EventService
    from app.services.job_service import JobService
    from app.services.message_service import MessageService
    from app.services.runtime_service import RuntimeService
    from app.services.session_auto_continue_service import SessionAutoContinueService
    from app.services.session_service import SessionService
    from app.services.tool_service import ToolService
    from app.services.workspace_service import WorkspaceService
    from app.services.agent_execution_service import AgentExecutionService
    from app.agents.agent_middleware import LLMLoggingMiddleware
    from app.core.job_event_bus import JobEventBus

_config_service: ConfigService | None = None
_agent_service: AgentService | None = None
_artifact_service: ArtifactService | None = None
_event_service: EventService | None = None
_job_service: JobService | None = None
_message_service: MessageService | None = None
_runtime_service: RuntimeService | None = None
_session_auto_continue_service: SessionAutoContinueService | None = None
_session_service: SessionService | None = None
_tool_service: ToolService | None = None
_workspace_service: WorkspaceService | None = None
_agent_execution_service: AgentExecutionService | None = None
_llm_logging_middleware: LLMLoggingMiddleware | None = None
_job_event_bus: JobEventBus | None = None


def init_app_services() -> None:
    """初始化应用级服务实例。"""
    global _config_service
    global _agent_service
    global _artifact_service
    global _event_service
    global _job_service
    global _message_service
    global _runtime_service
    global _session_auto_continue_service
    global _session_service
    global _tool_service
    global _workspace_service
    global _agent_execution_service
    global _llm_logging_middleware
    global _job_event_bus

    if _config_service is not None:
        return

    from app.services.config_service import ConfigService
    from app.services.agent_service import AgentService
    from app.services.artifact_service import ArtifactService
    from app.services.event_service import EventService
    from app.services.job_service import JobService
    from app.services.message_service import MessageService
    from app.services.runtime_service import RuntimeService
    from app.services.session_auto_continue_service import SessionAutoContinueService
    from app.services.session_service import SessionService
    from app.services.tool_service import ToolService
    from app.services.workspace_service import WorkspaceService
    from app.services.agent_execution_service import AgentExecutionService
    from app.agents.agent_middleware import LLMLoggingMiddleware
    from app.core.job_event_bus import JobEventBus
    from app.core.background_task_registry import BackgroundTaskRegistry
    from app.core.background_message_bus import BackgroundMessageBus

    job_event_bus = JobEventBus()
    background_task_registry = BackgroundTaskRegistry()
    background_message_bus = BackgroundMessageBus()

    _config_service = ConfigService()
    _agent_service = AgentService()
    _artifact_service = ArtifactService()
    _event_service = EventService()
    _job_service = JobService()
    _message_service = MessageService()
    _runtime_service = RuntimeService()
    _session_auto_continue_service = SessionAutoContinueService()
    _session_service = SessionService()
    _tool_service = ToolService()
    _workspace_service = WorkspaceService()
    _agent_execution_service = AgentExecutionService()
    _llm_logging_middleware = LLMLoggingMiddleware()
    _job_event_bus = job_event_bus

    # 将需要显式依赖的服务关联起来，避免在各自模块里再回退到运行时查找。
    _agent_execution_service.bind_config_service(_config_service)
    _agent_execution_service.bind_bus(job_event_bus)
    _job_service.bind_bus(job_event_bus)
    _job_service.bind_agent_execution_service(_agent_execution_service)
    _event_service.bind_bus(job_event_bus)
    _session_auto_continue_service.bind_dependencies(
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        session_service=_session_service,
        message_service=_message_service,
        job_service=_job_service,
        config_service=_config_service,
    )
    _session_service.bind_config_service(_config_service)
    _tool_service.bind_agent_execution_service(_agent_execution_service)
    _agent_service.bind_config_service(_config_service)
    _runtime_service.bind_job_service(_job_service)
    _workspace_service.bind_config_service(_config_service)
    _llm_logging_middleware.bind_job_event_bus(job_event_bus)


def clear_app_services() -> None:
    """清理应用级服务实例。"""
    global _config_service
    global _agent_service
    global _artifact_service
    global _event_service
    global _job_service
    global _message_service
    global _runtime_service
    global _session_auto_continue_service
    global _session_service
    global _tool_service
    global _workspace_service
    global _agent_execution_service
    global _llm_logging_middleware
    global _job_event_bus

    _config_service = None
    _agent_service = None
    _artifact_service = None
    _event_service = None
    _job_service = None
    _message_service = None
    _runtime_service = None
    _session_auto_continue_service = None
    _session_service = None
    _tool_service = None
    _workspace_service = None
    _agent_execution_service = None
    _llm_logging_middleware = None
    _job_event_bus = None


def get_config_service() -> ConfigService:
    if _config_service is None:
        raise RuntimeError("ConfigService 尚未初始化")
    return _config_service


def get_agent_service() -> AgentService:
    if _agent_service is None:
        raise RuntimeError("AgentService 尚未初始化")
    return _agent_service


def get_artifact_service() -> ArtifactService:
    if _artifact_service is None:
        raise RuntimeError("ArtifactService 尚未初始化")
    return _artifact_service


def get_event_service() -> EventService:
    if _event_service is None:
        raise RuntimeError("EventService 尚未初始化")
    return _event_service


def get_job_service() -> JobService:
    if _job_service is None:
        raise RuntimeError("JobService 尚未初始化")
    return _job_service


def get_message_service() -> MessageService:
    if _message_service is None:
        raise RuntimeError("MessageService 尚未初始化")
    return _message_service


def get_runtime_service() -> RuntimeService:
    if _runtime_service is None:
        raise RuntimeError("RuntimeService 尚未初始化")
    return _runtime_service


def get_session_auto_continue_service() -> SessionAutoContinueService:
    if _session_auto_continue_service is None:
        raise RuntimeError("SessionAutoContinueService 尚未初始化")
    return _session_auto_continue_service


def get_session_service() -> SessionService:
    if _session_service is None:
        raise RuntimeError("SessionService 尚未初始化")
    return _session_service


def get_tool_service() -> ToolService:
    if _tool_service is None:
        raise RuntimeError("ToolService 尚未初始化")
    return _tool_service


def get_workspace_service() -> WorkspaceService:
    if _workspace_service is None:
        raise RuntimeError("WorkspaceService 尚未初始化")
    return _workspace_service


def get_agent_execution_service() -> AgentExecutionService:
    if _agent_execution_service is None:
        raise RuntimeError("AgentExecutionService 尚未初始化")
    return _agent_execution_service


def get_job_event_bus() -> JobEventBus:
    if _job_event_bus is None:
        raise RuntimeError("JobEventBus 尚未初始化")
    return _job_event_bus
