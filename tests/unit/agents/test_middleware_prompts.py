from __future__ import annotations

from pathlib import Path

from deepagents.backends.state import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.summarization import SummarizationToolMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import SystemMessage

from app.agents.agent_factory import _team_aware_system_prompt
from app.agents.deep_agent_stack import build_deep_agent_middleware
from app.agents.middleware_prompts import (
    COMPACT_CONVERSATION_SYSTEM_PROMPT,
    FILESYSTEM_SYSTEM_PROMPT,
    FILESYSTEM_TOOL_DESCRIPTIONS,
    MEMORY_SYSTEM_PROMPT,
    SKILLS_SYSTEM_PROMPT,
    TODO_SYSTEM_PROMPT,
    TODO_TOOL_DESCRIPTION,
)
from app.agents.skill_runtime import WorkspaceSkillsMiddleware
from app.agents.tool_invocation_context import (
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)


def _build_middleware(
    tmp_path: Path,
    *,
    denylist: set[str] | None = None,
    skills: list[tuple[str, str]] | None = None,
    memory: list[str] | None = None,
) -> list[AgentMiddleware]:
    invocation_context = ToolInvocationContext()
    return build_deep_agent_middleware(
        model=FakeListChatModel(responses=["ok"]),
        backend=StateBackend(),
        workspace_root=tmp_path,
        permissions=None,
        resolved_skills=skills,
        resolved_tool_denylist=set(denylist or set()),
        interrupt_on=None,
        runtime_middleware=[],
        model_routing_middleware=None,
        tool_invocation_context_middleware=ToolInvocationContextMiddleware(
            invocation_context
        ),
        tool_output_middleware=AgentMiddleware(),
        memory=memory,
    )


def _find_middleware(
    middleware: list[AgentMiddleware],
    middleware_type: type[AgentMiddleware],
) -> AgentMiddleware:
    return next(item for item in middleware if isinstance(item, middleware_type))


def test_middleware_uses_project_prompts_without_upstream_demo_agents(tmp_path):
    middleware = _build_middleware(tmp_path, memory=["/memory.md"])

    todo = _find_middleware(middleware, TodoListMiddleware)
    assert todo.system_prompt == TODO_SYSTEM_PROMPT
    assert todo.tools[0].description == TODO_TOOL_DESCRIPTION

    filesystem = _find_middleware(middleware, FilesystemMiddleware)
    assert filesystem._custom_system_prompt == FILESYSTEM_SYSTEM_PROMPT
    assert {tool.name: tool.description for tool in filesystem.tools} == FILESYSTEM_TOOL_DESCRIPTIONS

    compact = _find_middleware(middleware, SummarizationToolMiddleware)
    assert compact.system_prompt == COMPACT_CONVERSATION_SYSTEM_PROMPT

    agent_memory = _find_middleware(middleware, MemoryMiddleware)
    assert agent_memory.system_prompt == MEMORY_SYSTEM_PROMPT


def test_workspace_skills_uses_compact_project_template():
    middleware = WorkspaceSkillsMiddleware(
        backend=StateBackend(),
        sources=[("/.boxteam/skills", "Workspace")],
    )

    assert middleware.system_prompt_template == SKILLS_SYSTEM_PROMPT
    assert "quantum computing" not in middleware.system_prompt_template


def test_denylist_removes_tool_specific_middleware_and_prompts(tmp_path):
    middleware = _build_middleware(
        tmp_path,
        denylist={"write_todos", "task", "compact_conversation", "read_file"},
        skills=[("/.boxteam/skills", "Workspace")],
    )

    assert not any(isinstance(item, TodoListMiddleware) for item in middleware)
    assert not any(isinstance(item, SummarizationToolMiddleware) for item in middleware)

    skills = _find_middleware(middleware, WorkspaceSkillsMiddleware)
    assert skills.system_prompt_template is None

    exposed_tool_names = {
        tool.name
        for item in middleware
        for tool in getattr(item, "tools", [])
    }
    assert not exposed_tool_names.intersection(
        {"write_todos", "task", "compact_conversation", "read_file"}
    )


def test_filesystem_middleware_is_omitted_when_all_its_tools_are_denied(tmp_path):
    middleware = _build_middleware(
        tmp_path,
        denylist=set(FILESYSTEM_TOOL_DESCRIPTIONS),
    )

    assert not any(isinstance(item, FilesystemMiddleware) for item in middleware)


def test_project_middleware_prompt_budget_stays_small(tmp_path):
    middleware = _build_middleware(tmp_path, memory=["/memory.md"])
    system_prompt_chars = 0
    tool_description_chars = 0
    for item in middleware:
        system_prompt = getattr(item, "system_prompt", None)
        if isinstance(system_prompt, str):
            system_prompt_chars += len(system_prompt)
        custom_system_prompt = getattr(item, "_custom_system_prompt", None)
        if isinstance(custom_system_prompt, str):
            system_prompt_chars += len(custom_system_prompt)
        tool_description_chars += sum(
            len(tool.description)
            for tool in getattr(item, "tools", [])
        )

    assert system_prompt_chars < 2_000
    assert tool_description_chars < 2_500


def test_team_system_prompt_forbids_polling_and_preserves_base_prompt():
    prompt = _team_aware_system_prompt("base prompt", enabled=True)

    assert isinstance(prompt, SystemMessage)
    assert "base prompt" in prompt.text
    assert "execute/sleep" in prompt.text
    assert "get_team_board once" in prompt.text
    assert "never claim that the team board is pending" in prompt.text
