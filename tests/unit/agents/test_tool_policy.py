from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from deepagents.backends.state import StateBackend
from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from app.agents.agent_tools import build_default_tools
from app.agents.deep_agent_stack import build_deep_agent_middleware
from app.agents.graph_tool_adapter import extract_agent_tools_by_name
from app.agents.policy import (
    DEFAULT_AGENT_TOOL_NAMES,
    build_agent_tool_universe,
    resolve_tool_policy,
    validate_tool_dependencies,
)
from app.agents.tool_invocation_context import (
    ToolInvocationContext,
    ToolInvocationContextMiddleware,
)


def test_default_policy_enables_entire_universe() -> None:
    universe = build_agent_tool_universe(extension_names={"web_search"})

    policy = resolve_tool_policy(
        universe_names=universe,
        extension_names={"web_search"},
    )

    assert policy.enabled_names == universe
    assert policy.disabled_names == frozenset()
    assert policy.enabled_extension_names == frozenset({"web_search"})


def test_explicit_deny_disables_only_named_tool() -> None:
    policy = resolve_tool_policy(
        universe_names=DEFAULT_AGENT_TOOL_NAMES,
        denylist={"edit_file"},
    )

    assert policy.disabled_names == frozenset({"edit_file"})
    assert policy.enabled_names == DEFAULT_AGENT_TOOL_NAMES - {"edit_file"}


def test_all_deny_with_single_allow_enables_only_restored_tool() -> None:
    policy = resolve_tool_policy(
        universe_names=DEFAULT_AGENT_TOOL_NAMES,
        denylist={"all"},
        allowlist={"read_file"},
    )

    assert policy.enabled_names == frozenset({"read_file"})
    assert policy.disabled_names == DEFAULT_AGENT_TOOL_NAMES - {"read_file"}


def test_extensions_deny_with_single_extension_allow_restores_one_child() -> None:
    extensions = {"web_search", "fetch_webpage"}
    universe = build_agent_tool_universe(extension_names=extensions)

    policy = resolve_tool_policy(
        universe_names=universe,
        extension_names=extensions,
        denylist={"extensions"},
        allowlist={"web_search"},
    )

    assert policy.enabled_extension_names == frozenset({"web_search"})
    assert "fetch_webpage" in policy.disabled_names
    assert "read_file" in policy.enabled_names


def test_delegation_tools_require_session_communication() -> None:
    with pytest.raises(
        ValueError,
        match="create_team_member, task 依赖 send_message_to_session",
    ):
        validate_tool_dependencies(
            DEFAULT_AGENT_TOOL_NAMES - {"send_message_to_session"},
            context="测试策略",
        )


def test_unknown_tool_name_fails_instead_of_silently_ignoring_typo() -> None:
    with pytest.raises(ValueError, match="不存在的工具: reed_file"):
        resolve_tool_policy(
            universe_names=DEFAULT_AGENT_TOOL_NAMES,
            denylist={"reed_file"},
        )


def test_policy_default_names_match_actual_agent_graph_tools(
    tmp_path: Path,
) -> None:
    invocation_context = ToolInvocationContext()
    direct_tools = build_default_tools(
        session_id="ses_policy_consistency",
        background_task_registry=MagicMock(),
        background_message_bus=MagicMock(),
        job_event_bus=MagicMock(),
        job_service=MagicMock(),
        message_service=MagicMock(),
        session_service=MagicMock(),
        session_orchestrator=MagicMock(),
        session_subagent_service=MagicMock(),
        team_service=MagicMock(),
        config_service=MagicMock(),
        terminal_manager_client=MagicMock(),
        invocation_context=invocation_context,
        workspace_root=tmp_path,
    )
    model = FakeListChatModel(responses=["ok"])
    middleware = build_deep_agent_middleware(
        model=model,
        backend=StateBackend(),
        workspace_root=tmp_path,
        permissions=None,
        resolved_skills=[],
        resolved_tool_denylist=set(),
        interrupt_on=None,
        runtime_middleware=[],
        model_routing_middleware=None,
        tool_invocation_context_middleware=ToolInvocationContextMiddleware(
            invocation_context
        ),
        tool_output_middleware=AgentMiddleware(),
        memory=None,
    )
    agent = create_agent(model, tools=direct_tools, middleware=middleware)

    actual_tool_names = frozenset(extract_agent_tools_by_name(agent))

    assert actual_tool_names == DEFAULT_AGENT_TOOL_NAMES
