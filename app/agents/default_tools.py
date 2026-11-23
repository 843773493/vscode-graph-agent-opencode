from __future__ import annotations

from typing import TYPE_CHECKING

from deepagents.middleware.permissions import FilesystemPermission
from langchain_core.tools import BaseTool

from app.abstractions.background_mailbox import BackgroundMailboxProtocol
from app.abstractions.communication import SessionCommunicationProtocol
from app.abstractions.session_subagent import SessionSubagentProtocol
from app.abstractions.team import TeamCoordinationProtocol
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.apply_patch import create_apply_patch_tool
from app.agents.tools.background import (
    create_background_message_collection_tool,
    create_system_time_emitter_tool,
)
from app.agents.tools.collaboration import build_agent_collaboration_tools
from app.agents.tools.goal import create_goal_tools
from app.agents.tools.python_execution import create_python_execution_tool
from app.agents.tools.terminal import (
    create_exec_command_tool,
    create_kill_terminal_tool,
    create_list_terminal_sessions_tool,
    create_write_stdin_tool,
)
from app.agents.tools.testing import create_test_tool
from app.agents.tools.user_questions import create_ask_user_async_tool
from app.services.business.terminal_resources.owner import TerminalResourceOwner
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from app.services.orchestration.background_work.registry import BackgroundTaskRegistry

if TYPE_CHECKING:
    from app.services.business.communication.user_questions import UserQuestionService
    from app.services.business.session_goal_service import SessionGoalService


def build_default_tools(
    session_id: str | None,
    agent_id: str = "default",
    *,
    text_mutations: WorkspaceTextMutationOwner,
    background_task_registry: BackgroundTaskRegistry,
    background_mailbox: BackgroundMailboxProtocol,
    session_subagent_service: SessionSubagentProtocol | None = None,
    team_service: TeamCoordinationProtocol | None = None,
    terminal_resource_owner: TerminalResourceOwner | None = None,
    invocation_context: ToolInvocationContext | None = None,
    goal_service: SessionGoalService | None = None,
    user_question_service: UserQuestionService | None = None,
    user_facing: bool = True,
    session_communication_service: SessionCommunicationProtocol | None = None,
    include_test_tools: bool = False,
    include_team_tools: bool = False,
    filesystem_permissions: list[FilesystemPermission] | None = None,
) -> list[BaseTool]:
    """装配固定默认工具；策略过滤由调用方负责。"""
    if session_subagent_service is None:
        raise RuntimeError("build_default_tools 需要显式传入 SessionSubagentService")
    if include_team_tools and team_service is None:
        raise RuntimeError("build_default_tools 需要显式传入 TeamCoordinationService")
    if terminal_resource_owner is None:
        raise RuntimeError("build_default_tools 需要显式传入 TerminalResourceOwner")
    if invocation_context is None:
        raise RuntimeError("build_default_tools 需要显式传入 ToolInvocationContext")
    tools = [
        create_apply_patch_tool(text_mutations=text_mutations, permissions=filesystem_permissions),
        create_python_execution_tool(session_id=session_id, agent_id=agent_id),
        create_system_time_emitter_tool(
            session_id=session_id,
            agent_id=agent_id,
            background_task_registry=background_task_registry,
            invocation_context=invocation_context,
        ),
        create_background_message_collection_tool(
            session_id=session_id,
            agent_id=agent_id,
            background_mailbox=background_mailbox,
            invocation_context=invocation_context,
        ),
        create_exec_command_tool(
            session_id=session_id,
            agent_id=agent_id,
            terminal_owner=terminal_resource_owner,
            invocation_context=invocation_context,
        ),
        create_write_stdin_tool(
            session_id=session_id,
            terminal_owner=terminal_resource_owner,
            invocation_context=invocation_context,
        ),
        create_list_terminal_sessions_tool(
            session_id=session_id,
            terminal_owner=terminal_resource_owner,
            invocation_context=invocation_context,
        ),
        create_kill_terminal_tool(
            session_id=session_id,
            terminal_owner=terminal_resource_owner,
            invocation_context=invocation_context,
        ),
        *build_agent_collaboration_tools(
            session_id=session_id,
            agent_id=agent_id,
            session_subagent_service=session_subagent_service,
            team_service=team_service,
            invocation_context=invocation_context,
            session_communication_service=session_communication_service,
            include_team_tools=include_team_tools,
        ),
    ]
    if goal_service is not None:
        tools.extend(
            create_goal_tools(session_id=session_id, goal_service=goal_service, invocation_context=invocation_context)
        )
    if user_question_service is not None and user_facing:
        tools.append(create_ask_user_async_tool(user_question_service, invocation_context))
    if include_test_tools:
        tools.insert(0, create_test_tool())
    return tools
