from __future__ import annotations

from app.agents.tools.team import create_team_tools
from app.agents.tool_invocation_context import ToolInvocationContext


class _UnusedTeamService:
    pass


def test_default_team_tool_group_exposes_board_and_session_reuse_operations():
    tools = create_team_tools(
        session_id="ses_current",
        agent_id="default",
        team_service=_UnusedTeamService(),
        invocation_context=ToolInvocationContext(),
    )

    assert [tool.name for tool in tools] == [
        "create_team",
        "list_my_teams",
        "get_team_board",
        "create_team_member",
        "attach_team_session",
        "assign_team_task",
        "update_team_task",
    ]
    schemas = {tool.name: tool.args for tool in tools}
    assert "startup_prompt" in schemas["create_team_member"]
    assert "session_id" not in schemas["create_team_member"]
    assert "session_id" in schemas["attach_team_session"]
    assert schemas["assign_team_task"]["cycle"]["minimum"] == 1
    create_member = next(tool for tool in tools if tool.name == "create_team_member")
    assert "runtime" not in create_member.get_input_schema().model_fields
    assert "monitor_session_agent_end" in create_member.description
    assign_task = next(tool for tool in tools if tool.name == "assign_team_task")
    assert "不要阻塞轮询" in assign_task.description
