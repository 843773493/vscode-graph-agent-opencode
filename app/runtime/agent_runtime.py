from __future__ import annotations

from typing import Any, Protocol

from app.agents.agent_factory import create_runtime_deep_agent_for_session, resolve_agent_id
from app.services.config_service import ConfigService
from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import JobEventBus


class AgentRuntimeDependencyProvider(Protocol):
    def get_message_service(self) -> Any: ...

    def get_session_service(self) -> Any: ...


def build_session_agent_runtime(
    *,
    session_id: str,
    agent_id: str,
    config_service: ConfigService,
    background_task_registry: BackgroundTaskRegistry,
    background_message_bus: BackgroundMessageBus,
    job_event_bus: JobEventBus,
    dependency_provider: AgentRuntimeDependencyProvider,
    name: str | None = None,
) -> Any:
    resolved_agent_id = resolve_agent_id(agent_id, config_service)
    return create_runtime_deep_agent_for_session(
        session_id=session_id,
        agent_id=resolved_agent_id,
        config_service=config_service,
        background_task_registry=background_task_registry,
        background_message_bus=background_message_bus,
        job_event_bus=job_event_bus,
        message_service=dependency_provider.get_message_service(),
        session_service=dependency_provider.get_session_service(),
        name=name or resolved_agent_id,
    )