from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agents import agent_factory
from app.agents.instruction_producers import runtime_instruction_producers
from app.agents.policy import ToolPolicyResolver
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)


@pytest.fixture
def graph_observations(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []

    def observe(operation_id: str, phase: str, *, details: Mapping[str, object] | None = None) -> None:
        observations.append({"operation_id": operation_id, "phase": phase, "details": details})

    monkeypatch.setattr(agent_factory, "observe_phase_sync", observe)
    return observations


def test_agent_factory_keeps_empty_extension_envelope_after_tool_filter(
    tmp_path: Path,
    graph_observations: list[dict[str, object]],
) -> None:
    """扩展目录为空且过滤掉信封时，Provider 仍收到固定入口。"""

    config_service = MagicMock()
    config_service.get_tool_policy_resolver.return_value = ToolPolicyResolver(
        policy_defaults={},
        policy_rules={},
        restrictions={},
    )
    built_agent = MagicMock()
    configured_agent = MagicMock()
    built_agent.with_config.return_value = configured_agent

    required = {
        "model": MagicMock(),
        "instruction_producers": runtime_instruction_producers(
            agent_id="default", content="测试提示词", config_revision="config:1",
            runtime_identity_description="测试身份说明", provider_candidates=(("fixture", "model"),),
        ),
        "tool_selection_reader": ToolSelectionStore(boxteam_root=tmp_path / ".boxteam"),
        "checkpointer": MagicMock(),
        "session_id": "ses_empty_extension_catalog",
        "thread_id": "thr_00000000400070008000000000000002",
        "agent_id": "default",
        "tools": [],
        "enabled_tool_names": {"read_file"},
        "background_task_registry": MagicMock(),
        "background_message_bus": MagicMock(),
        "job_event_bus": MagicMock(),
        "job_service": MagicMock(),
        "message_service": MagicMock(),
        "session_service": MagicMock(),
        "session_orchestrator": MagicMock(),
        "session_subagent_service": MagicMock(),
        "team_service": MagicMock(),
        "session_context_query_service": MagicMock(),
        "workspace_session_context_client": MagicMock(),
        "config_service": config_service,
        "resource_registry": ResourceRegistry(),
        "resource_activation": None,
        "resource_navigation": MagicMock(),
        "text_mutations": MagicMock(),
        "workspace_root": tmp_path,
        "skill_catalog": MagicMock(),
        "middleware": [],
    }

    with (
        patch.object(agent_factory, "ContextSourceManager", return_value=ContextSourceManager()),
        patch.object(agent_factory, "build_workspace_backend"),
        patch.object(agent_factory, "append_skill_middlewares"),
        patch.object(agent_factory, "build_deep_agent_middleware", return_value=[]),
        patch.object(agent_factory, "ToolOutputMiddleware"),
        patch.object(agent_factory, "ToolInvocationContextMiddleware"),
        patch.object(agent_factory, "ModelCallExecution"),
        patch.object(
            agent_factory, "create_model_call_agent", return_value=built_agent
        ) as create_agent,
        patch.object(agent_factory.GRAPH_FACTORY_REGISTRY, "resolve"),
        patch.object(
            agent_factory,
            "create_extension_tool_invoker_tool",
            wraps=agent_factory.create_extension_tool_invoker_tool,
        ) as create_invoker,
    ):
        result = agent_factory.create_my_deep_agent(**required)

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
    assert [tool.name for tool in provider_tools] == [EXTENSION_TOOL_INVOKER_NAME]


def test_agent_factory_omits_all_csm_ports_without_durable_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合成 session 没有 durable owner 时不装配 CSM 持久端口或事件发布。"""

    config_service = MagicMock()
    config_service.get_tool_policy_resolver.return_value = ToolPolicyResolver(
        policy_defaults={},
        policy_rules={},
        restrictions={},
    )
    built_agent = MagicMock()
    built_agent.with_config.return_value = built_agent
    checkpointer = RolloutCheckpointSaver(tmp_path / "sessions")
    monkeypatch.setenv("BOXTEAM_HOME", str(tmp_path / "boxteam-home"))
    resource_registry = ResourceRegistry()
    file_registry = WorkspaceFileResourceRegistry(
        workspace_root=tmp_path,
        watch_service=WorkspaceFileWatchService(workspace_root=tmp_path),
    )
    required = {
        "model": MagicMock(),
        "instruction_producers": runtime_instruction_producers(
            agent_id="default", content="测试提示词", config_revision="config:1",
            runtime_identity_description="测试身份说明", provider_candidates=(("fixture", "model"),),
        ),
        "tool_selection_reader": ToolSelectionStore(boxteam_root=tmp_path / ".boxteam"),
        "checkpointer": checkpointer,
        "session_id": "tools_inspection_session",
        "owns_thread": False,
        "thread_id": "thr_00000000400070008000000000000002",
        "agent_id": "default",
        "tools": [],
        "enabled_tool_names": {"read_file"},
        "background_task_registry": MagicMock(),
        "background_message_bus": MagicMock(),
        "job_event_bus": MagicMock(),
        "job_service": MagicMock(),
        "message_service": MagicMock(),
        "session_service": MagicMock(),
        "session_orchestrator": MagicMock(),
        "session_subagent_service": MagicMock(),
        "team_service": MagicMock(),
        "session_context_query_service": MagicMock(),
        "workspace_session_context_client": MagicMock(),
        "config_service": config_service,
        "resource_registry": resource_registry,
        "resource_activation": None,
        "resource_navigation": MagicMock(),
        "text_mutations": MagicMock(),
        "workspace_root": tmp_path,
        "workspace_file_resource_registry": file_registry,
        "middleware": [],
    }

    with (
        patch.object(
            checkpointer,
            "context_source_control_owner_available",
            return_value=False,
        ),
        patch.object(agent_factory, "ContextSourceManager", wraps=ContextSourceManager) as context_manager,
        patch.object(agent_factory, "build_workspace_backend"),
        patch.object(agent_factory, "build_workspace_skill_catalog") as build_catalog,
        patch.object(agent_factory, "append_skill_middlewares"),
        patch.object(
            agent_factory, "build_deep_agent_middleware", return_value=[]
        ) as build_middleware,
        patch.object(agent_factory, "ToolOutputMiddleware"),
        patch.object(agent_factory, "ToolInvocationContextMiddleware"),
        patch.object(agent_factory, "ContextSourceReactor") as reactor,
        patch.object(agent_factory, "ModelCallExecution"),
        patch.object(agent_factory, "create_model_call_agent", return_value=built_agent),
        patch.object(agent_factory.GRAPH_FACTORY_REGISTRY, "resolve"),
    ):
        gateway_token = set_current_gateway_id("gateway_ed824856cf39565635b0e5a6bf8e08da")
        try:
            agent_factory.create_my_deep_agent(**required)
        finally:
            reset_current_gateway_id(gateway_token)

    call = context_manager.call_args.kwargs
    assert call["owner"] is None
    assert call["control_state_port"] is None
    assert call["lifecycle_event_sink"] is None
    reactor.assert_not_called()
    build_catalog.assert_not_called()
    middleware_kwargs = build_middleware.call_args.kwargs
    assert middleware_kwargs["resolved_skills"] is None
    assert middleware_kwargs["source_registry"] is None
    assert middleware_kwargs["context_source_reactor"] is None
