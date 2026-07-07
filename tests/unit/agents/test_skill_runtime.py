from __future__ import annotations

from unittest.mock import MagicMock

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

from app.agents.agent_tools import create_test_tool, create_test_tool_2
from app.agents.skill_tools import SkillToolFactoryContext
from app.agents.skill_runtime import (
    SkillToolExposureMiddleware,
    WorkspaceSkillsMiddleware,
    _activated_hidden_tool_names,
    discover_workspace_skill_tool_map,
    discover_workspace_skill_sources,
)


def _skill_tool_context(tmp_path) -> SkillToolFactoryContext:
    return SkillToolFactoryContext(
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


def test_activated_hidden_tool_names_uses_skill_read_file_call(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_read_skill",
                    "name": "read_file",
                    "args": {
                        "file_path": "/.boxteam/skills/test-tool-skill/SKILL.md",
                    },
                    "type": "tool_call",
                }
            ],
        )
    ]
    skills_metadata = [
        {
            "name": "test-tool-skill",
            "path": "/.boxteam/skills/test-tool-skill/SKILL.md",
            "allowed_tools": ["test_tool_2"],
        }
    ]

    assert _activated_hidden_tool_names(
        messages=messages,
        skills_metadata=skills_metadata,
        hidden_tool_names={"test_tool_2"},
    ) == {"test_tool_2"}


def test_discover_workspace_skill_tool_map_reads_allowed_tools(tmp_path):
    skill_dir = tmp_path / ".boxteam" / "skills" / "test-tool-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-tool-skill\n"
        "description: Test skill for hidden tool loading.\n"
        "allowed-tools: test_tool_2\n"
        "---\n"
        "# Test\n",
        encoding="utf-8",
    )

    assert discover_workspace_skill_tool_map(tmp_path) == {
        "test_tool_2": ["test-tool-skill"],
    }


def test_discover_workspace_skill_tool_map_filters_to_hidden_tools(tmp_path):
    skill_dir = tmp_path / ".boxteam" / "skills" / "test-tool-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-tool-skill\n"
        "description: Test skill for hidden tool loading.\n"
        "allowed-tools: test_tool_2 python_exec\n"
        "---\n"
        "# Test\n",
        encoding="utf-8",
    )

    assert discover_workspace_skill_tool_map(
        tmp_path,
        hidden_tool_names={"test_tool_2"},
    ) == {
        "test_tool_2": ["test-tool-skill"],
    }


def test_workspace_skills_prompt_hides_allowed_tools(tmp_path):
    skill_dir = tmp_path / ".boxteam" / "skills" / "test-tool-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-tool-skill\n"
        "description: Test skill for hidden validation.\n"
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
                "name": "test-tool-skill",
                "description": "Test skill for hidden validation.",
                "path": "/.boxteam/skills/test-tool-skill/SKILL.md",
                "metadata": {},
                "license": None,
                "compatibility": None,
                "allowed_tools": ["test_tool_2"],
            }
        ]
    )

    assert "test-tool-skill" in skill_list
    assert "/.boxteam/skills/test-tool-skill/SKILL.md" in skill_list
    assert "test_tool_2" not in skill_list


def test_skill_tool_exposure_middleware_hides_tool_until_skill_is_read(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    middleware = SkillToolExposureMiddleware(hidden_tool_names={"test_tool_2"})
    visible_tool = create_test_tool()
    hidden_tool = create_test_tool_2(_skill_tool_context(tmp_path))
    captured_tool_names: list[list[str]] = []

    def handler(request: ModelRequest):
        captured_tool_names.append([tool.name for tool in request.tools])
        return ModelResponse(result=[AIMessage(content="ok")])

    base_state = {
        "messages": [],
        "skills_metadata": [
            {
                "name": "test-tool-skill",
                "path": "/.boxteam/skills/test-tool-skill/SKILL.md",
                "allowed_tools": ["test_tool_2"],
            }
        ],
    }
    initial_request = ModelRequest(
        model=None,
        messages=[],
        tools=[visible_tool, hidden_tool],
        state=base_state,
    )

    middleware.wrap_model_call(initial_request, handler)

    activated_request = initial_request.override(
        messages=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_read_skill",
                        "name": "read_file",
                        "args": {
                            "file_path": "/.boxteam/skills/test-tool-skill/SKILL.md",
                        },
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    middleware.wrap_model_call(activated_request, handler)

    assert captured_tool_names == [
        ["test_tool"],
        ["test_tool", "test_tool_2"],
    ]
