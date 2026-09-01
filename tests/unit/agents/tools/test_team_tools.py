from __future__ import annotations

from unittest.mock import MagicMock

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.policy import DIRECT_AGENT_COLLABORATION_TOOL_NAMES
from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.collaboration import build_agent_collaboration_tools
from app.agents.tools.team import create_team_tools


class _UnusedTeamService:
    pass


def test_agent_collaboration_group_collects_cross_session_tools():
    tools = build_agent_collaboration_tools(
        session_id="ses_current",
        agent_id="default",
        sender_agent_id="default",
        background_task_registry=MagicMock(),
        background_message_bus=MagicMock(),
        job_event_bus=MagicMock(),
        job_service=MagicMock(),
        session_service=MagicMock(),
        session_orchestrator=MagicMock(),
        session_subagent_service=MagicMock(),
        team_service=MagicMock(),
        invocation_context=ToolInvocationContext(),
        include_team_tools=True,
    )

    assert {tool.name for tool in tools} == set(
        DIRECT_AGENT_COLLABORATION_TOOL_NAMES
    )


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
    assert schemas["update_team_task"]["team_id"]["pattern"] == r"^team_[0-9a-f]{32}$"


def test_invalid_team_id_is_returned_to_model_as_a_schema_error() -> None:
    class _ToolBindingFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    tools = create_team_tools(
        session_id="ses_current",
        agent_id="default",
        team_service=_UnusedTeamService(),
        invocation_context=ToolInvocationContext(),
    )
    model = _ToolBindingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "update_team_task",
                        "args": {
                            "team_id": "x",
                            "task_id": "task_1",
                            "status": "completed",
                            "summary": "已完成",
                        },
                        "id": "call-invalid-team-id",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="已跳过无效团队调用并完成报告。"),
        ]
    )
    agent = create_agent(model, tools=tools)

    result = agent.invoke({"messages": [HumanMessage(content="完成任务")]})

    tool_message = next(
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    )
    assert tool_message.status == "error"
    assert tool_message.tool_call_id == "call-invalid-team-id"
    assert "team_id" in tool_message.content
    assert result["messages"][-1].content == "已跳过无效团队调用并完成报告。"
