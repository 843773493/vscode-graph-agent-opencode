from __future__ import annotations

from app.agents.default_tools import build_default_tools
from app.agents.tool_invocation_context import ToolInvocationContext


class _UnusedBackgroundService:
    """仅构造工具定义；实际执行由后台工具测试覆盖。"""


class _FakeSessionSubagentService:
    async def delegate(self, **kwargs):
        raise AssertionError(f"本测试不应执行 task 工具: {kwargs}")


class _FakeTeamService:
    pass


class _UnusedTerminalOwner:
    pass


def test_agent_includes_background_message_collection_tool(
    text_mutations_factory, communication_probe, empty_workspace_scope,
):
    workspace_root = empty_workspace_scope[1]
    background_mailbox = _UnusedBackgroundService()
    background_task_registry = _UnusedBackgroundService()

    tools = build_default_tools(text_mutations=text_mutations_factory(workspace_root), session_communication_service=communication_probe,
        session_id="ses_019bca51019973818b63e9d44c0e94f5",
        agent_id="default",
        background_task_registry=background_task_registry,
        background_mailbox=background_mailbox,
        session_subagent_service=_FakeSessionSubagentService(),
        team_service=_FakeTeamService(),
        terminal_resource_owner=_UnusedTerminalOwner(),
        invocation_context=ToolInvocationContext(),
        include_test_tools=True,
        include_team_tools=True,
    )

    tool_names = [tool.name for tool in tools]
    assert "test_tool" in tool_names
    assert "apply_patch" in tool_names
    assert "python_exec" in tool_names
    assert "emit_system_time_messages" in tool_names
    assert "wait_for_session" in tool_names
    assert "collect_background_messages" in tool_names
    assert "exec_command" in tool_names
    assert "write_stdin" in tool_names
    assert "list_terminal_sessions" in tool_names
    assert "kill_terminal" in tool_names
    assert "send_message_to_session" in tool_names
    assert "task" in tool_names
    assert "create_team" in tool_names
    assert "attach_team_session" not in tool_names
    assert "assign_team_task" in tool_names
    assert "update_team_task" in tool_names
    assert len(tools) == 18


def test_default_tools_omit_disabled_test_tool(
    text_mutations_factory, communication_probe, empty_workspace_scope,
):
    workspace_root = empty_workspace_scope[1]
    tools = build_default_tools(text_mutations=text_mutations_factory(workspace_root), session_communication_service=communication_probe,
        session_id="session_production",
        background_task_registry=_UnusedBackgroundService(),
        background_mailbox=_UnusedBackgroundService(),
        session_subagent_service=_FakeSessionSubagentService(),
        team_service=_FakeTeamService(),
        terminal_resource_owner=_UnusedTerminalOwner(),
        invocation_context=ToolInvocationContext(),
        include_team_tools=True,
    )

    assert "test_tool" not in {tool.name for tool in tools}


def test_single_agent_tool_set_omits_team_board_tools(
    text_mutations_factory, communication_probe, empty_workspace_scope,
):
    workspace_root = empty_workspace_scope[1]
    tools = build_default_tools(text_mutations=text_mutations_factory(workspace_root), session_communication_service=communication_probe,
        session_id="session_single_agent",
        background_task_registry=_UnusedBackgroundService(),
        background_mailbox=_UnusedBackgroundService(),
        session_subagent_service=_FakeSessionSubagentService(),
        terminal_resource_owner=_UnusedTerminalOwner(),
        invocation_context=ToolInvocationContext(),
        include_team_tools=False,
    )

    tool_names = {tool.name for tool in tools}
    assert tool_names.isdisjoint(
        {
            "create_team",
            "list_my_teams",
            "get_team_board",
            "create_team_member",
            "assign_team_task",
            "update_team_task",
        }
    )
    assert {"send_message_to_session", "task"} <= tool_names


def test_default_tool_order_keeps_enabled_test_and_team_tools(
    text_mutations_factory, communication_probe, empty_workspace_scope,
):
    workspace_root = empty_workspace_scope[1]

    tools = build_default_tools(text_mutations=text_mutations_factory(workspace_root), session_communication_service=communication_probe,
        session_id="session_tool_order",
        agent_id="default",
        background_task_registry=_UnusedBackgroundService(),
        background_mailbox=_UnusedBackgroundService(),
        session_subagent_service=_FakeSessionSubagentService(),
        team_service=_FakeTeamService(),
        terminal_resource_owner=_UnusedTerminalOwner(),
        invocation_context=ToolInvocationContext(),
        include_test_tools=True,
        include_team_tools=True,
    )

    direct_tool_names = [tool.name for tool in tools]
    assert direct_tool_names == [
        "test_tool",
        "apply_patch",
        "python_exec",
        "emit_system_time_messages",
        "collect_background_messages",
        "exec_command",
        "write_stdin",
        "list_terminal_sessions",
        "kill_terminal",
        "wait_for_session",
        "send_message_to_session",
        "task",
        "create_team",
        "list_my_teams",
        "get_team_board",
        "create_team_member",
        "assign_team_task",
        "update_team_task",
    ]
