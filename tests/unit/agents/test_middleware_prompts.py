from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from deepagents.backends.state import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.summarization import SummarizationToolMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from app.agents.deep_agent_stack import build_deep_agent_middleware
from app.agents.instruction_producers import runtime_instruction_producers
from app.agents.middleware_prompts import (
    COMPACT_CONVERSATION_SYSTEM_PROMPT,
    FILESYSTEM_SYSTEM_PROMPT,
    FILESYSTEM_TOOL_DESCRIPTIONS,
    SKILLS_SYSTEM_PROMPT,
    TEAM_COORDINATION_SYSTEM_PROMPT,
    TODO_SYSTEM_PROMPT,
    TODO_TOOL_DESCRIPTION,
)
from app.agents.skill_runtime import (
    PublishedSkillCatalog,
    WorkspaceSkillsMiddleware,
    build_workspace_skill_catalog,
)
from app.agents.structured_prompt_validation_middleware import (
    StructuredPromptValidationMiddleware,
)
from app.agents.summarization.preflight import NoDurableOwnerCompactionPreflight
from app.agents.tool_invocation_context import (
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)


@pytest.fixture
def demo_skill_catalog(tmp_path: Path) -> PublishedSkillCatalog:
    """在隔离工作区构建装配测试所需的最小 SkillCatalog。"""
    skill_dir = tmp_path / ".boxteam" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 装配测试 Skill\n---\n# demo\n",
        encoding="utf-8",
    )
    return build_workspace_skill_catalog(
        tmp_path,
        registry=ResourceRegistry(),
        file_registry=WorkspaceFileResourceRegistry(
            workspace_root=tmp_path,
            watch_service=WorkspaceFileWatchService(workspace_root=tmp_path),
            project_root=tmp_path,
        ),
        project_root=tmp_path,
    )


def _build_middleware(
    tmp_path: Path,
    *,
    denylist: set[str] | None = None,
    resolved_skill_catalog: PublishedSkillCatalog | None = None,
    context_source_manager: ContextSourceManager | None = None,
) -> list[AgentMiddleware]:
    invocation_context = ToolInvocationContext()
    return build_deep_agent_middleware(
        model=FakeListChatModel(responses=["ok"]),
        backend=StateBackend(),
        workspace_root=tmp_path,
        resource_navigation=MagicMock(),
        artifact_reader=MagicMock(),
        text_mutations=MagicMock(),
        permissions=None,
        resolved_skills=resolved_skill_catalog,
        context_source_manager=context_source_manager or ContextSourceManager(),
        compaction_preflight=NoDurableOwnerCompactionPreflight(),
        resolved_tool_denylist=set(denylist or set()),
        interrupt_on=None,
        runtime_middleware=[],
        model_routing_middleware=None,
        tool_invocation_context_middleware=ToolInvocationContextMiddleware(
            invocation_context
        ),
        tool_output_middleware=AgentMiddleware(),
    )


def _find_middleware(
    middleware: list[AgentMiddleware],
    middleware_type: type[AgentMiddleware],
) -> AgentMiddleware:
    return next(item for item in middleware if isinstance(item, middleware_type))


def test_middleware_uses_project_prompts_without_upstream_demo_agents(tmp_path):
    manager = ContextSourceManager()
    middleware = _build_middleware(tmp_path, context_source_manager=manager)
    sources = {source.descriptor.source_id: source for source in manager.prepare_sources().sources}
    assert sources["instruction:todo-list"].content == TODO_SYSTEM_PROMPT
    assert sources["instruction:filesystem-rules"].content == FILESYSTEM_SYSTEM_PROMPT
    assert sources["instruction:compact-tool-usage"].content == COMPACT_CONVERSATION_SYSTEM_PROMPT

    _find_middleware(middleware, StructuredPromptValidationMiddleware)

    todo = _find_middleware(middleware, TodoListMiddleware)
    assert todo.system_prompt == ""
    assert todo.tools[0].description == TODO_TOOL_DESCRIPTION

    filesystem = _find_middleware(middleware, FilesystemMiddleware)
    assert filesystem._custom_system_prompt == ""
    assert {tool.name: tool.description for tool in filesystem.tools} == FILESYSTEM_TOOL_DESCRIPTIONS
    assert "execute" not in {tool.name for tool in filesystem.tools}
    read_file = next(tool for tool in filesystem.tools if tool.name == "read_file")
    assert set(read_file.tool_call_schema.model_json_schema()["properties"]) == {
        "path",
        "line_offset",
        "max_lines",
        "max_bytes",
        "max_response_bytes",
        "timeout_ms",
    }

    compact = _find_middleware(middleware, SummarizationToolMiddleware)
    assert compact.system_prompt is None


def test_workspace_skills_uses_compact_project_template(
    demo_skill_catalog: PublishedSkillCatalog,
) -> None:
    middleware = WorkspaceSkillsMiddleware(
        catalog=demo_skill_catalog,
    )

    assert middleware.system_prompt_template == SKILLS_SYSTEM_PROMPT
    assert "quantum computing" not in middleware.system_prompt_template


@pytest.mark.parametrize("read_enabled", [False, True])
@pytest.mark.parametrize("skill_enabled", [False, True])
def test_skill_catalog_prompt_depends_on_skill_load_instead_of_file_reading(
    tmp_path: Path,
    demo_skill_catalog: PublishedSkillCatalog,
    read_enabled: bool,
    skill_enabled: bool,
) -> None:
    manager = ContextSourceManager()
    denied = {
        name for name, enabled in (
            ("read_file", read_enabled), ("skill_load", skill_enabled),
        ) if not enabled
    }
    middleware = _build_middleware(
        tmp_path,
        denylist=denied,
        resolved_skill_catalog=demo_skill_catalog,
        context_source_manager=manager,
    )
    skills = _find_middleware(middleware, WorkspaceSkillsMiddleware)
    skills.before_agent({}, None, {})
    sources = {
        source.descriptor.source_id: source
        for source in manager.prepare_sources().sources
    }
    assert ("instruction:skill-catalog" in sources) is skill_enabled
    if skill_enabled:
        assert "装配测试 Skill" in sources["instruction:skill-catalog"].content
        assert 'skill_load(name="...")' in sources["instruction:skill-catalog"].content
        assert "# demo" not in sources["instruction:skill-catalog"].content


@pytest.mark.parametrize("deny_history", [False, True])
def test_denylist_removes_tool_specific_middleware_and_prompts(
    tmp_path: Path,
    deny_history: bool,
    demo_skill_catalog: PublishedSkillCatalog,
) -> None:
    manager = ContextSourceManager()
    denied = {"write_todos", "task", "compact_conversation", "read_file", "skill_load"}
    if deny_history:
        denied.add("read_compaction_history")
    middleware = _build_middleware(
        tmp_path,
        denylist=denied,
        resolved_skill_catalog=demo_skill_catalog,
        context_source_manager=manager,
    )

    assert not any(isinstance(item, TodoListMiddleware) for item in middleware)
    compact = _find_middleware(middleware, SummarizationToolMiddleware)
    assert {tool.name for tool in compact.tools} == (
        set() if deny_history else {"read_compaction_history"}
    )
    sources = {source.descriptor.source_id for source in manager.prepare_sources().sources}
    assert "instruction:compact-tool-usage" not in sources
    assert "instruction:todo-list" not in sources

    skills = _find_middleware(middleware, WorkspaceSkillsMiddleware)
    assert skills.system_prompt_template is None

    exposed_tool_names = {
        tool.name
        for item in middleware
        for tool in getattr(item, "tools", [])
    }
    assert not exposed_tool_names.intersection(denied)


def test_filesystem_middleware_is_omitted_when_all_its_tools_are_denied(tmp_path):
    middleware = _build_middleware(
        tmp_path,
        denylist=set(FILESYSTEM_TOOL_DESCRIPTIONS),
    )

    assert not any(isinstance(item, FilesystemMiddleware) for item in middleware)


def test_project_middleware_prompt_budget_stays_small(tmp_path):
    middleware = _build_middleware(tmp_path)
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


def test_team_source_rules_forbid_polling():
    assert "exec_command/sleep" in TEAM_COORDINATION_SYSTEM_PROMPT
    assert "get_team_board once" in TEAM_COORDINATION_SYSTEM_PROMPT
    assert "never claim that the team board is pending" in TEAM_COORDINATION_SYSTEM_PROMPT


def test_runtime_identity_separates_model_from_project_and_uses_relative_workspace_scope():
    import json

    producers = runtime_instruction_producers(
        agent_id="default", content="base prompt", config_revision="config:1",
        runtime_identity_description="测试身份说明", provider_candidates=(
            ("backup_3", "gpt-5.6-luna"),
            ("backup_4", "gpt-5.6-luna"),
        ),
    )

    by_id = {producer.producer_id: producer for producer in producers}
    assert by_id["R01"].content == "base prompt"
    identity = json.loads(by_id["R02"].content)
    assert identity["text"] == "测试身份说明"
    assert identity["runtime_facts"] == {
        "agent_id": "default", "workspace_root": ".",
        "identity_scope": "configured_candidates_not_actual_call",
        "configured_candidates": [
            {"provider_id": "backup_3", "model_id": "gpt-5.6-luna"},
            {"provider_id": "backup_4", "model_id": "gpt-5.6-luna"},
        ],
    }


def test_build_runtime_injects_selected_provider_and_relative_workspace_scope(
    monkeypatch,
):
    from unittest.mock import MagicMock

    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.agents import agent_factory

    config_service = MagicMock()
    config_service.get_revision.return_value = "config:runtime-identity"
    config_service.get_agent_runtime_config.return_value = {
        "system_prompt": "base prompt",
        "runtime_identity_description": "测试身份说明",
        "providers": [
            {
                "id": "backup_3",
                "model": "gpt-5.6-luna",
                "api_mode": {
                    "protocol": "chat_completions",
                    "supports_reasoning": {},
                    "request_features": {},
                    "replay_policy": {},
                },
            },
            {
                "id": "backup_4",
                "model": "gpt-5.6-luna",
                "api_mode": {
                    "protocol": "chat_completions",
                    "supports_reasoning": {},
                    "request_features": {},
                    "replay_policy": {},
                },
            },
        ],
    }
    monkeypatch.setattr(
        agent_factory,
        "build_model_from_provider",
        lambda provider, runtime_config, prompt_cache_key=None: FakeListChatModel(
            responses=["ok"]
        ),
    )

    runtime = agent_factory.build_runtime_for_agent(
        "default",
        config_service,
    )

    import json

    identity = next(item for item in runtime["instruction_producers"] if item.producer_id == "R02")
    facts = json.loads(identity.content)["runtime_facts"]
    assert facts["workspace_root"] == "."
    assert facts["configured_candidates"][0] == {"provider_id": "backup_3", "model_id": "gpt-5.6-luna"}
