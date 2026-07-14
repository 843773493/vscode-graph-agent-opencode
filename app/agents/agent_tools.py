from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from app.abstractions.background_message_bus import BackgroundMessageBusProtocol
from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.agents.tools.background import (
    create_background_message_collection_tool,
    create_monitor_session_agent_end_tool,
    create_system_time_emitter_tool,
)
from app.agents.tools.apply_patch import create_apply_patch_tool
from app.agents.tools.python_execution import (
    create_python_execution_tool,
    get_python_executable,
)
from app.agents.tools.session_messaging import create_send_message_to_session_tool
from app.agents.tools.terminal import create_persistent_terminal_tool
from app.agents.tools.testing import create_test_tool
from app.core.background_task_registry import BackgroundTaskRegistry
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient


def build_default_tools(
    session_id: str,
    agent_id: str = "default",
    sender_agent_id: str = "default",
    *,
    background_task_registry: BackgroundTaskRegistry,
    background_message_bus: BackgroundMessageBusProtocol,
    job_event_bus: JobEventBusProtocol,
    job_service: JobServiceProtocol,
    session_orchestrator: SessionOrchestratorProtocol | None = None,
    message_service: Any | None = None,
    session_service: Any | None = None,
    config_service: Any | None = None,
    terminal_manager_client: TerminalManagerClient | None = None,
    workspace_root: Path | None = None,
) -> list[BaseTool]:
    """构建默认工具集。"""
    if session_orchestrator is None:
        raise RuntimeError("build_default_tools 需要显式传入 SessionOrchestrator")
    if message_service is None:
        raise RuntimeError("build_default_tools 需要显式传入 MessageService")
    if session_service is None:
        raise RuntimeError("build_default_tools 需要显式传入 SessionService")
    if config_service is None:
        raise RuntimeError("build_default_tools 需要显式传入 ConfigService")
    if terminal_manager_client is None:
        raise RuntimeError("build_default_tools 需要显式传入 TerminalManagerClient")
    return [
        create_test_tool(),
        create_apply_patch_tool(workspace_root=workspace_root),
        create_python_execution_tool(session_id=session_id, agent_id=agent_id),
        create_system_time_emitter_tool(
            session_id=session_id,
            agent_id=agent_id,
            background_task_registry=background_task_registry,
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
        create_background_message_collection_tool(
            session_id=session_id,
            agent_id=agent_id,
            background_message_bus=background_message_bus,
        ),
        create_persistent_terminal_tool(
            session_id=session_id,
            agent_id=agent_id,
            terminal_client=terminal_manager_client,
        ),
        create_send_message_to_session_tool(
            sender_session_id=session_id,
            sender_agent_id=sender_agent_id,
            session_orchestrator=session_orchestrator,
        ),
    ]


__all__ = [
    "build_default_tools",
    "create_apply_patch_tool",
    "create_background_message_collection_tool",
    "create_monitor_session_agent_end_tool",
    "create_persistent_terminal_tool",
    "create_python_execution_tool",
    "create_send_message_to_session_tool",
    "create_system_time_emitter_tool",
    "create_test_tool",
    "get_python_executable",
]
