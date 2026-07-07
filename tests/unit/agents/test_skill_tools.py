from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.agents.skill_tools import (
    SkillToolFactoryContext,
    build_skill_tools,
    skill_tool_spec_names,
)


TEST_TOOL_SPEC = {
    "name": "test_tool_2",
    "factory": "app.agents.agent_tools:create_test_tool_2",
}


def _context() -> SkillToolFactoryContext:
    return SkillToolFactoryContext(
        session_id="ses_test",
        agent_id="default",
        sender_agent_id="default",
        workspace_root=Path.cwd(),
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


def test_build_skill_tools_loads_factory_from_config_spec() -> None:
    tools = build_skill_tools([TEST_TOOL_SPEC], context=_context())

    assert [tool.name for tool in tools] == ["test_tool_2"]
    assert tools[0].invoke({}) == "4568"


def test_skill_tool_spec_names_reads_names_from_config_spec() -> None:
    assert skill_tool_spec_names([TEST_TOOL_SPEC]) == {"test_tool_2"}


def test_build_skill_tools_rejects_legacy_name_only_config() -> None:
    with pytest.raises(ValueError, match="不再支持只写工具名"):
        build_skill_tools(["test_tool_2"], context=_context())
