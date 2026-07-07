from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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
    )


def test_discover_workspace_skill_sources_requires_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    assert discover_workspace_skill_sources(tmp_path) == []

    (tmp_path / ".boxteam" / "skills").mkdir(parents=True)

    assert discover_workspace_skill_sources(tmp_path) == [
        ("/.boxteam/skills", "Workspace")
    ]


def test_workspace_agents_middleware_injects_root_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# 工作区指令\n\n必须优先读取对应 skill。\n",
        encoding="utf-8",
    )
    middleware = WorkspaceAgentsMiddleware(workspace_root=tmp_path)
    request = MagicMock()
    request.system_message = None
    request.override.side_effect = lambda **kwargs: kwargs

    modified = middleware.modify_request(request)
    system_message = modified["system_message"]
    system_text = "\n".join(
        block.get("text", "")
        for block in system_message.content_blocks
        if isinstance(block, dict)
    )

    assert "Workspace AGENTS.md" in system_text
    assert "<workspace_agents_md path=\"/AGENTS.md\">" in system_text
    assert "必须优先读取对应 skill。" in system_text


def test_workspace_agents_middleware_skips_missing_agents_md(tmp_path):
    middleware = WorkspaceAgentsMiddleware(workspace_root=tmp_path)
    request = MagicMock()

    assert middleware.modify_request(request) is request


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
