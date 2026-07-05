from __future__ import annotations

from typing import Any, Protocol

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.agents.agent_factory import create_runtime_deep_agent_for_session, resolve_agent_id
from app.services.infrastructure.config_service import ConfigService
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry

from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.core.path_utils import get_checkpoints_dir


class AgentRuntimeDependencyProvider(Protocol):
    def get_message_service(self) -> Any: ...

    def get_session_service(self) -> Any: ...

    def get_session_orchestrator(self) -> Any: ...

    def get_checkpointer(self) -> Any: ...


def build_session_agent_runtime(
    *,
    session_id: str,
    agent_id: str,
    config_service: ConfigService,
    background_task_registry: BackgroundTaskRegistry,
    background_message_bus: BackgroundMessageBus,
    job_event_bus: JobEventBusProtocol,
    dependency_provider: AgentRuntimeDependencyProvider,
    name: str | None = None,
    override_model: Any = None,
    fallback_middleware_enabled: bool = True,
) -> Any:
    resolved_agent_id = resolve_agent_id(agent_id, config_service)
    checkpointer = getattr(dependency_provider, "get_checkpointer", lambda: None)()
    if checkpointer is None:
        checkpointer = FileSystemCheckpointSaver(base_dir=get_checkpoints_dir())
    return create_runtime_deep_agent_for_session(
        session_id=session_id,
        agent_id=resolved_agent_id,
        config_service=config_service,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        message_service=dependency_provider.get_message_service(),
        session_service=dependency_provider.get_session_service(),
        session_orchestrator=dependency_provider.get_session_orchestrator(),
        checkpointer=checkpointer,
        name=name or resolved_agent_id,
        override_model=override_model,
        fallback_middleware_enabled=fallback_middleware_enabled,
    )
