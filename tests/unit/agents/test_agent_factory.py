from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import agent_factory
from app.agents.instruction_producers import runtime_instruction_producers
from app.agents.summarization.tool_middleware import CompactionToolsMiddleware
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME, TOOL_DISCOVERY_NAME
from app.agents.tool_node_adapter import extract_agent_tools_by_name
from app.core.path_utils import workspace_context
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore


@pytest.fixture
def factory_workspace(tmp_path: Path) -> Iterator[Path]:
    with workspace_context(load_or_create_workspace_id(tmp_path), tmp_path):
        yield tmp_path


@pytest.fixture
async def factory_config(
    factory_workspace: Path, request: pytest.FixtureRequest,
) -> AsyncIterator[ConfigService]:
    source = factory_workspace / ".boxteam" / "workspace.jsonc"
    source.write_text(json.dumps({
        "config_version": 4,
        "extends": "builtin:workspace",
        "settings": {"workspace": {
            "context": {"compaction": {
                "prepare_fraction": 0.6, "apply_fraction": 0.8,
                "target_fraction": 0.4, "apply_policy": "next_request",
            }},
            "ui": {"auto_summarize": getattr(request, "param", False)},
        }},
    }), encoding="utf-8")
    service = ConfigService(config_dir=Path.cwd() / "configs", workspace_root=factory_workspace)
    try:
        yield service
    finally:
        await service.close()


@pytest.fixture
def graph_observations(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []

    def observe(operation_id: str, phase: str, *, details: Mapping[str, object] | None = None) -> None:
        observations.append({"operation_id": operation_id, "phase": phase, "details": details})

    monkeypatch.setattr(agent_factory, "observe_phase_sync", observe)
    return observations


def test_agent_factory_keeps_empty_extension_envelope_after_tool_filter(
    factory_workspace: Path,
    factory_config: ConfigService,
    graph_observations: list[dict[str, object]],
) -> None:
    """扩展目录为空且过滤掉信封时，Provider 仍收到固定入口。"""

    built_agent = MagicMock()
    configured_agent = MagicMock()
    built_agent.with_config.return_value = configured_agent

    required = {
        "model": MagicMock(),
        "instruction_producers": runtime_instruction_producers(
            agent_id="default", content="测试提示词", config_revision="config:1",
            runtime_identity_description="测试身份说明", provider_candidates=(("fixture", "model"),),
        ),
        "tool_selection_reader": ToolSelectionStore(boxteam_root=factory_workspace / ".boxteam"),
        "checkpointer": MagicMock(),
        "session_id": "ses_empty_extension_catalog",
        "thread_id": "thr_00000000400070008000000000000002",
        "agent_id": "default",
        "tools": [],
        "enabled_tool_names": {"read_file"},
        "background_task_registry": MagicMock(),
        "background_mailbox": MagicMock(),
        "job_event_bus": MagicMock(),
        "job_service": MagicMock(),
        "message_service": MagicMock(),
        "session_service": MagicMock(),
        "session_orchestrator": MagicMock(),
        "session_subagent_service": MagicMock(),
        "team_service": MagicMock(),
        "session_context_query_service": MagicMock(),
        "workspace_session_context_client": MagicMock(),
        "config_service": factory_config,
        "resource_registry": ResourceRegistry(),
        "resource_activation": None,
        "resource_navigation": MagicMock(),
        "text_mutations": MagicMock(),
        "workspace_root": factory_workspace,
        "skill_catalog": MagicMock(),
        "middleware": [],
    }

    with (
        patch.object(agent_factory, "ContextSourceManager", return_value=ContextSourceManager()),
        patch.object(agent_factory, "build_workspace_backend"),
        patch.object(agent_factory, "build_deep_agent_middleware", return_value=[]),
        patch.object(agent_factory, "ToolOutputMiddleware"),
        patch.object(agent_factory, "ToolInvocationContextMiddleware"),
        patch.object(agent_factory, "ModelCallExecution"),
        patch.object(agent_factory, "prepare_agent_model", side_effect=AssertionError("override 不应构造配置模型")),
        patch.object(
            agent_factory, "create_model_call_agent", return_value=built_agent
        ) as create_agent,
        patch.object(
            agent_factory,
            "create_extension_tool_invoker_tool",
            wraps=agent_factory.create_extension_tool_invoker_tool,
        ) as create_invoker,
    ):
        result = agent_factory.assemble_deep_agent(**required)

    assert result is configured_agent
    assert graph_observations == [{
        "operation_id": required["thread_id"], "phase": "agent.graph.compiled",
        "details": {
            "session_id": required["session_id"], "thread_id": required["thread_id"],
            "owns_thread": True, "compiled_graph_object_id": id(built_agent),
            "compiled_graph_type": f"{type(built_agent).__module__}.{type(built_agent).__qualname__}",
            "agent_object_id": id(configured_agent), "scope_object_id": None,
        },
    }], "观察必须区分真正的编译对象与执行使用的配置 wrapper"
    create_invoker.assert_called_once()
    assert create_invoker.call_args.args[0] == []
    provider_tools = create_agent.call_args.kwargs["tools"]
    assert create_agent.call_args.args[0] is required["model"]
    assert [tool.name for tool in provider_tools] == [EXTENSION_TOOL_INVOKER_NAME, TOOL_DISCOVERY_NAME]



@pytest.fixture
def local_factory_arguments(factory_workspace: Path, factory_config: ConfigService):
    from types import SimpleNamespace

    from app.agents.policy import parse_custom_tool_specs

    root = factory_workspace
    session = MagicMock()
    thread = "thr_00000000400070008000000000000002"
    session.path_resolver.main_thread_id.return_value = thread
    return {
        "model": None, "instruction_producers": (), "tool_selection_reader": ToolSelectionStore(boxteam_root=root / ".boxteam"),
        "session_id": "ses_local_factory", "thread_id": thread, "agent_id": "default", "checkpointer": MagicMock(),
        "custom_tool_specs": parse_custom_tool_specs([{"name": "dynamic", "factory": "builtins:str"}]),
        "config_service": factory_config, "session_service": session, "background_task_registry": MagicMock(),
        "background_mailbox": MagicMock(), "job_event_bus": MagicMock(), "job_service": MagicMock(), "message_service": MagicMock(),
        "session_orchestrator": MagicMock(), "session_subagent_service": MagicMock(), "terminal_resource_owner": MagicMock(),
        "browser_resource_owner": MagicMock(), "session_communication_service": MagicMock(),
        "session_context_query_service": MagicMock(), "workspace_session_context_client": MagicMock(),
        "resource_registry": ResourceRegistry(), "resource_activation": None, "workspace_root": root,
        "resource_navigation": MagicMock(), "text_mutations": MagicMock(),
        "skill_catalog": SimpleNamespace(entries=(), sources=(), gateway_id=None),
    }


@pytest.mark.parametrize(
    "factory_config,automatic_enabled", [(False, False), (True, True)],
    indirect=["factory_config"], ids=["automatic-disabled", "automatic-enabled"],
)
def test_agent_factory_registers_compaction_tools_without_a_durable_owner(
    local_factory_arguments, automatic_enabled: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实配置、middleware 与图编译贯通；不执行模型或工具。"""
    local_factory_arguments.update(
        model=FakeListChatModel(responses=[], profile={"max_input_tokens": 1000}),
        checkpointer=InMemorySaver(), custom_tool_specs=(), tools=[], middleware=[],
        instruction_producers=runtime_instruction_producers(
            agent_id="default", content="测试提示词", config_revision="config:1",
            runtime_identity_description="测试身份说明", provider_candidates=(("fixture", "model"),),
        ),
    )
    monkeypatch.setattr(
        agent_factory, "prepare_agent_model",
        lambda **_: pytest.fail("显式模型的装配不能构造 Provider 模型"),
    )
    with (
        patch.object(
            agent_factory, "build_deep_agent_middleware",
            wraps=agent_factory.build_deep_agent_middleware,
        ) as build_middleware,
        patch.object(
            agent_factory, "create_model_call_agent", wraps=agent_factory.create_model_call_agent,
        ) as create_agent,
    ):
        agent = agent_factory.assemble_deep_agent(**local_factory_arguments)

    build_middleware.assert_called_once()
    options = build_middleware.call_args.kwargs
    assert options["compaction_runtime"] is None
    assert options["compaction_owner"] == MutationIntentOwner(local_factory_arguments["session_id"], local_factory_arguments["thread_id"])
    assert options["agent_id"] == "default"
    create_agent.assert_called_once()
    summarization, = (
        item for item in create_agent.call_args.kwargs["middleware"]
        if isinstance(item, CompactionToolsMiddleware)
    )
    assert summarization._compactor is None
    assert local_factory_arguments["config_service"].is_automatic_compaction_enabled() is automatic_enabled
    assert "model" in agent.get_graph().nodes
    assert {"compact_conversation", "read_compaction_history"} <= set(extract_agent_tools_by_name(agent))


def test_local_factory_error_precedes_model_construction_and_graph(local_factory_arguments, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("本地 factory 失败不能继续构造默认工具、来源管理器、模型或图")

    monkeypatch.setattr(agent_factory, "build_default_tools", forbidden)
    monkeypatch.setattr(agent_factory, "ContextSourceManager", forbidden)
    monkeypatch.setattr(agent_factory, "prepare_agent_model", forbidden)
    monkeypatch.setattr(agent_factory, "create_model_call_agent", forbidden)
    with pytest.raises(TypeError, match="dynamic"):
        agent_factory.assemble_deep_agent(**local_factory_arguments)


def test_dynamic_factory_constructed_once_before_model_failure(local_factory_arguments, monkeypatch):
    from langchain_core.tools import StructuredTool

    built = []
    failure = RuntimeError("选定模型构造失败")

    def construct(context):
        built.append(context.invocation_context.thread_binding)
        return StructuredTool(name="dynamic", description="动态工具", args_schema={"type": "object"})

    def fail_model(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("app.agents.custom_tools.load_custom_tool_factory", lambda _: construct)
    monkeypatch.setattr(agent_factory, "prepare_agent_model", fail_model)
    with pytest.raises(RuntimeError) as observed:
        agent_factory.assemble_deep_agent(**local_factory_arguments)
    assert observed.value is failure
    assert [(binding.session_id, binding.thread_id) for binding in built] == [
        (local_factory_arguments["session_id"], local_factory_arguments["thread_id"]),
    ]


@pytest.mark.parametrize("tool_name", [
    "exec_command", "read_file", "write_todos", "skill_load",
    "compact_conversation", "read_compaction_history",
])
@pytest.mark.parametrize("hidden", [False, True])
def test_builtin_collision_still_fails_before_model(local_factory_arguments, monkeypatch, tool_name, hidden):
    from langchain_core.tools import StructuredTool

    from app.agents.policy import parse_custom_tool_specs

    local_factory_arguments["custom_tool_specs"] = parse_custom_tool_specs([
        {"name": tool_name, "factory": "builtins:str"},
    ])
    local_factory_arguments["tool_denylist"] = {tool_name} if hidden else set()
    monkeypatch.setattr("app.agents.custom_tools.load_custom_tool_factory", lambda _: lambda context: StructuredTool(
        name=tool_name, description="与内置注册重名", args_schema={"type": "object"},
    ))
    monkeypatch.setattr(agent_factory, "prepare_agent_model", lambda **_: pytest.fail("注册冲突不能准备模型"))
    with pytest.raises(ValueError, match="重复工具名"):
        agent_factory.assemble_deep_agent(**local_factory_arguments)


@pytest.mark.parametrize("conflict", [False, True])
def test_runtime_middleware_tools_checked_before_model(local_factory_arguments, monkeypatch, conflict):
    from jsonschema.exceptions import SchemaError
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain_core.tools import StructuredTool

    item = AgentMiddleware()
    item.tools = [StructuredTool(
        name="read_file" if conflict else "runtime_tool",
        description="运行中间件工具",
        args_schema={"type": "object"} if conflict else {"type": "invalid"},
    )]
    local_factory_arguments.update(custom_tool_specs=(), middleware=[item])
    monkeypatch.setattr(agent_factory, "prepare_agent_model", lambda **_: pytest.fail("本地注册错误不能准备模型"))
    monkeypatch.setattr(agent_factory, "create_model_call_agent", lambda **_: pytest.fail("本地注册错误不能编译图"))
    with pytest.raises(ValueError if conflict else SchemaError):
        agent_factory.assemble_deep_agent(**local_factory_arguments)
