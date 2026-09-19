from __future__ import annotations

from langchain_core.tools import BaseTool

from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_message import SessionMessageDeliveryProtocol
from app.abstractions.session_orchestrator import SessionOrchestratorProtocol
from app.abstractions.session_subagent import (
    SessionReaderProtocol,
    SessionSubagentProtocol,
)
from app.abstractions.team import TeamCoordinationProtocol
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.session_messaging import create_send_message_to_session_tool
from app.agents.tools.session_subagent import create_session_subagent_tool
from app.agents.tools.session_wait import (
    CommunicationWaitBindingLookupPort,
    create_wait_for_session_tool,
)
from app.agents.tools.team import create_team_tools
from app.core.background_task_registry import BackgroundTaskRegistry


def build_agent_collaboration_tools(
    *,
    session_id: str,
    agent_id: str,
    sender_agent_id: str,
    background_task_registry: BackgroundTaskRegistry,
    job_service: JobServiceProtocol,
    session_service: SessionReaderProtocol,
    session_orchestrator: SessionOrchestratorProtocol,
    session_subagent_service: SessionSubagentProtocol,
    team_service: TeamCoordinationProtocol | None,
    invocation_context: ToolInvocationContext,
    session_message_delivery_service: SessionMessageDeliveryProtocol | None = None,
    communication_binding_lookup: CommunicationWaitBindingLookupPort | None = None,
    include_team_tools: bool = False,
) -> list[BaseTool]:
    """构建跨 Session 协作工具；团队面板工具按运行模式显式启用。"""
    if communication_binding_lookup is None:
        raise RuntimeError(
            "启用 wait_for_session 必须显式传入 communication binding lookup"
        )
    tools = [
        create_wait_for_session_tool(
            session_id=session_id,
            agent_id=agent_id,
            job_service=job_service,
            binding_lookup=communication_binding_lookup,
        ),
        create_send_message_to_session_tool(
            sender_session_id=session_id,
            sender_agent_id=sender_agent_id,
            session_orchestrator=session_orchestrator,
            message_delivery_service=session_message_delivery_service,
        ),
        create_session_subagent_tool(
            parent_session_id=session_id,
            parent_agent_id=agent_id,
            session_subagent_service=session_subagent_service,
            invocation_context=invocation_context,
        ),
    ]
    if include_team_tools:
        if team_service is None:
            raise RuntimeError("启用团队工具时必须显式传入 TeamCoordinationService")
        tools.extend(
            create_team_tools(
                session_id=session_id,
                agent_id=agent_id,
                team_service=team_service,
                invocation_context=invocation_context,
            )
        )
    return tools
