from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage

from app.agents.custom_tools import CustomToolFactoryContext
from app.agents.tools.testing import create_test_tool_2
from app.agents.skill_runtime import (
    WorkspaceAgentsMiddleware,
    WorkspaceSkillsMiddleware,
    discover_workspace_custom_tool_skill_map,
    discover_workspace_skill_sources,
)
from app.agents.tool_identity import CUSTOM_TOOL_INVOKER_NAME
from app.agents.tools.custom_invocation import create_custom_tool_invoker_tool


def _custom_tool_context(tmp_path) -> CustomToolFactoryContext:
    return CustomToolFactoryContext(
        session_id="ses_test",
        agent_id="default",
        sender_agent_id="default",
        workspace_root=tmp_path,
        background_task_registry=MagicMock(),
        background_message_bus=MagicMock(),
        job_event_bus=MagicMock(),
        job_service=MagicMock(),
        message_service=MagicMock(),
        session_service=MagicMock(),
        session_orchestrator=MagicMock(),
        config_service=MagicMock(),
        terminal_manager_client=MagicMock(),
        browser_manager_client=MagicMock(),
    )


def test_discover_workspace_skill_sources_requires_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    assert discover_workspace_skill_sources(tmp_path) == []

    (tmp_path / ".boxteam" / "skills").mkdir(parents=True)

    assert discover_workspace_skill_sources(tmp_path) == [
        ("/.boxteam/skills", "Workspace")
    ]


def _initialize_workspace_agents_state(
    middleware: WorkspaceAgentsMiddleware,
) -> dict:
    state = {"messages": []}
    update = middleware.before_model(state, MagicMock())
    assert update is not None
    state.update(update)
    return state


def _workspace_agents_system_text(
    middleware: WorkspaceAgentsMiddleware,
    state: dict,
) -> str:
    request = MagicMock()
    request.state = state
    request.system_message = None
    request.override.side_effect = lambda **kwargs: kwargs

    modified = middleware.modify_request(request)
    system_message = modified["system_message"]
    return "\n".join(
        block.get("text", "")
        for block in system_message.content_blocks
        if isinstance(block, dict)
    )


def test_workspace_agents_middleware_injects_frozen_root_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# 工作区指令\n\n必须优先读取对应 skill。\n",
        encoding="utf-8",
    )
    middleware = WorkspaceAgentsMiddleware(workspace_root=tmp_path)
    state = _initialize_workspace_agents_state(middleware)
    system_text = _workspace_agents_system_text(middleware, state)

    assert "Workspace AGENTS.md" in system_text
    assert "<workspace_agents_md path=\"/AGENTS.md\">" in system_text
    assert "必须优先读取对应 skill。" in system_text


def test_workspace_agents_middleware_skips_missing_agents_md(tmp_path):
    middleware = WorkspaceAgentsMiddleware(workspace_root=tmp_path)
    request = MagicMock()
    request.state = _initialize_workspace_agents_state(middleware)

    assert middleware.modify_request(request) is request


def test_workspace_agents_middleware_appends_change_reminder_without_replacing_system_prompt(
    tmp_path,
):
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text("# 指令\n\n使用旧规则。\n", encoding="utf-8")
    middleware = WorkspaceAgentsMiddleware(workspace_root=tmp_path)
    state = _initialize_workspace_agents_state(middleware)
    original_system_text = _workspace_agents_system_text(middleware, state)

    agents_path.write_text("# 指令\n\n使用新规则。\n", encoding="utf-8")
    update = middleware.before_model(state, MagicMock())

    assert update is not None
    reminder_messages = update["messages"]
    assert len(reminder_messages) == 1
    reminder = reminder_messages[0]
    assert isinstance(reminder, HumanMessage)
    assert "<system_reminder>" in reminder.text
    assert "workspace_agents_md_change" in reminder.text
    assert "+使用新规则。" in reminder.text
    assert "-使用旧规则。" in reminder.text

    state.update({key: value for key, value in update.items() if key != "messages"})
    state["messages"].extend(reminder_messages)
    assert _workspace_agents_system_text(middleware, state) == original_system_text
    assert middleware.before_model(state, MagicMock()) is None


def test_workspace_agents_middleware_reloads_latest_version_after_compaction(tmp_path):
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text("# 指令\n\n使用旧规则。\n", encoding="utf-8")
    middleware = WorkspaceAgentsMiddleware(workspace_root=tmp_path)
    state = _initialize_workspace_agents_state(middleware)

    agents_path.write_text("# 指令\n\n使用新规则。\n", encoding="utf-8")
    change_update = middleware.before_model(state, MagicMock())
    assert change_update is not None
    state.update(
        {key: value for key, value in change_update.items() if key != "messages"}
    )
    state["messages"].extend(change_update["messages"])
    state["_summarization_event"] = {
        "cutoff_index": 1,
        "summary_message": HumanMessage(content="已压缩历史"),
        "file_path": "/.boxteam/conversation_history/ses_test.md",
    }

    compact_update = middleware.before_model(state, MagicMock())

    assert compact_update is not None
    assert "messages" not in compact_update
    state.update(compact_update)
    system_text = _workspace_agents_system_text(middleware, state)
    assert "使用新规则。" in system_text
    assert "使用旧规则。" not in system_text


def test_discover_workspace_custom_tool_skill_map_reads_allowed_tools(tmp_path):
    skill_dir = tmp_path / ".boxteam" / "skills" / "test-tool-2"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-tool-2\n"
        "description: Test skill for custom tool loading.\n"
        "allowed-tools: test_tool_2\n"
        "---\n"
        "# Test\n",
        encoding="utf-8",
    )

    assert discover_workspace_custom_tool_skill_map(tmp_path) == {
        "test_tool_2": ["test-tool-2"],
    }


def test_discover_workspace_custom_tool_skill_map_filters_to_configured_custom_tools(tmp_path):
    skill_dir = tmp_path / ".boxteam" / "skills" / "test-tool-2"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-tool-2\n"
        "description: Test skill for custom tool loading.\n"
        "allowed-tools: test_tool_2 python_exec\n"
        "---\n"
        "# Test\n",
        encoding="utf-8",
    )

    assert discover_workspace_custom_tool_skill_map(
        tmp_path,
        custom_tool_names={"test_tool_2"},
    ) == {
        "test_tool_2": ["test-tool-2"],
    }


def test_workspace_skills_prompt_keeps_custom_tools_out_of_skill_list(tmp_path):
    skill_dir = tmp_path / ".boxteam" / "skills" / "test-tool-2"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-tool-2\n"
        "description: Test skill for custom validation.\n"
        "allowed-tools: test_tool_2\n"
        "---\n"
        "# Test\n"
        "调用 `test_tool_2`。\n",
        encoding="utf-8",
    )
    middleware = WorkspaceSkillsMiddleware(
        backend=MagicMock(),
        sources=[("/.boxteam/skills", "Workspace")],
    )
    skill_list = middleware._format_skills_list(
        [
            {
                "name": "test-tool-2",
                "description": "Test skill for custom validation.",
                "path": "/.boxteam/skills/test-tool-2/SKILL.md",
                "metadata": {},
                "license": None,
                "compatibility": None,
                "allowed_tools": ["test_tool_2"],
            }
        ]
    )

    assert "test-tool-2" in skill_list
    assert "/.boxteam/skills/test-tool-2/SKILL.md" in skill_list
    assert "用户请求匹配本 skill 描述时，先读取" in skill_list
    assert "test_tool_2" not in skill_list


@pytest.mark.asyncio
async def test_custom_tool_invoker_dispatches_configured_tool_without_skill_activation(tmp_path):
    custom_tool = create_test_tool_2(_custom_tool_context(tmp_path))
    invoker = create_custom_tool_invoker_tool([custom_tool])

    result = await invoker.ainvoke(
        {
            "tool_name": "test_tool_2",
            "arguments": {},
        }
    )

    assert invoker.name == CUSTOM_TOOL_INVOKER_NAME
    assert set(invoker.args) == {"tool_name", "arguments"}
    assert result == "4568"
