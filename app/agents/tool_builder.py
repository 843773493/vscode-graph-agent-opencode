from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from langchain_core.tools import BaseTool

from app.core.background_message_bus import BackgroundMessageBus
from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.job_event_bus import JobEventBus
from app.services.config_service import ConfigService

if TYPE_CHECKING:
    from app.services.job_service import JobService
    from app.services.message_service import MessageService
    from app.services.session_service import SessionService


def build_default_tools(
    *,
    session_id: str,
    agent_id: str,
    sender_agent_id: str,
    background_task_registry: BackgroundTaskRegistry,
    background_message_bus: BackgroundMessageBus,
    job_event_bus: JobEventBus,
    job_service: "JobService",
    message_service: "MessageService",
    session_service: "SessionService",
    config_service: ConfigService,
) -> list[BaseTool | Callable[..., Any] | dict[str, Any]]:
    from app.agents.agent_tools import (
        create_background_message_collection_tool,
        create_monitor_session_agent_end_tool,
        create_send_message_to_session_tool,
        create_system_time_emitter_tool,
        create_python_execution_tool,
    )

    tools: list[BaseTool | Callable[..., Any] | dict[str, Any]] = [
        create_system_time_emitter_tool(
            session_id=session_id,
            agent_id=agent_id,
            background_message_bus=background_message_bus,
        ),
        create_monitor_session_agent_end_tool(
            session_id=session_id,
            agent_id=agent_id,
            background_task_registry=background_task_registry,
            background_message_bus=background_message_bus,
            job_event_bus=job_event_bus,
            job_service=job_service,
        ),
        create_send_message_to_session_tool(
            sender_agent_id=sender_agent_id,
            job_service=job_service,
            message_service=message_service,
            session_service=session_service,
            config_service=config_service,
            job_event_bus=job_event_bus,
        ),
        create_python_execution_tool(session_id=session_id, agent_id=agent_id),
        create_background_message_collection_tool(
            session_id=session_id,
            agent_id=agent_id,
            background_message_bus=background_message_bus,
        ),
    ]
    return tools