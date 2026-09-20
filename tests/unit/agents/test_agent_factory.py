from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from app.agents import agent_factory
from app.agents.policy import ToolPolicyResolver
from app.agents.tool_identity import EXTENSION_TOOL_INVOKER_NAME
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)


def test_agent_factory_keeps_empty_extension_envelope_after_tool_filter(
    tmp_path: Path,
) -> None:
    """扩展目录为空且过滤掉信封时，Provider 仍收到固定入口。"""

    config_service = MagicMock()
    config_service.get_tool_policy_resolver.return_value = ToolPolicyResolver(
        policy_defaults={},
        policy_rules={},
        restrictions={},
    )
    built_agent = MagicMock()
    built_agent.with_config.return_value = built_agent

    required = {
        "model": MagicMock(),
        "system_prompt": "测试提示词",
        "checkpointer": MagicMock(),
        "session_id": "ses_empty_extension_catalog",
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
        "session_context_query_service": MagicMock(),
        "workspace_session_context_client": MagicMock(),
        "config_service": config_service,
        "workspace_root": tmp_path,
        "skill_catalog": MagicMock(),
        "middleware": [],
    }

    with (
        patch.object(agent_factory, "ContextSourceManager", return_value=None),
        patch.object(agent_factory, "build_workspace_backend"),
        patch.object(agent_factory, "append_skill_middlewares"),
        patch.object(agent_factory, "build_deep_agent_middleware", return_value=[]),
        patch.object(agent_factory, "ToolOutputMiddleware"),
        patch.object(agent_factory, "ToolInvocationContextMiddleware"),
        patch.object(agent_factory, "SealedAssemblyDispatchBridge"),
        patch.object(agent_factory, "create_agent", return_value=built_agent) as create_agent,
        patch.object(agent_factory.GRAPH_FACTORY_REGISTRY, "resolve"),
        patch.object(
            agent_factory,
            "create_extension_tool_invoker_tool",
            wraps=agent_factory.create_extension_tool_invoker_tool,
        ) as create_invoker,
    ):
        result = agent_factory.create_my_deep_agent(**required)

    assert result is built_agent
    create_invoker.assert_called_once()
    assert create_invoker.call_args.args[0] == []
    provider_tools = create_agent.call_args.kwargs["tools"]
    assert [tool.name for tool in provider_tools] == [EXTENSION_TOOL_INVOKER_NAME]


def test_agent_factory_omits_all_csm_ports_without_durable_owner(
    tmp_path: Path,
) -> None:
    """合成 session 没有 durable owner 时 CSM 三个端口必须全部为空。"""

    config_service = MagicMock()
    config_service.get_tool_policy_resolver.return_value = ToolPolicyResolver(
        policy_defaults={},
        policy_rules={},
        restrictions={},
    )
    built_agent = MagicMock()
    built_agent.with_config.return_value = built_agent
    checkpointer = RolloutCheckpointSaver(tmp_path / "sessions")

    required = {
        "model": MagicMock(),
        "system_prompt": "测试提示词",
        "checkpointer": checkpointer,
        "session_id": "tools_inspection_session",
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
        "session_context_query_service": MagicMock(),
        "workspace_session_context_client": MagicMock(),
        "config_service": config_service,
        "workspace_root": tmp_path,
        "skill_catalog": MagicMock(),
        "middleware": [],
    }

    with (
        patch.object(
            checkpointer,
            "context_source_control_owner_available",
            return_value=False,
        ),
        patch.object(agent_factory, "ContextSourceManager") as context_manager,
        patch.object(agent_factory, "build_workspace_backend"),
        patch.object(agent_factory, "append_skill_middlewares"),
        patch.object(agent_factory, "build_deep_agent_middleware", return_value=[]),
        patch.object(agent_factory, "ToolOutputMiddleware"),
        patch.object(agent_factory, "ToolInvocationContextMiddleware"),
        patch.object(agent_factory, "SealedAssemblyDispatchBridge"),
        patch.object(agent_factory, "create_agent", return_value=built_agent),
        patch.object(agent_factory.GRAPH_FACTORY_REGISTRY, "resolve"),
    ):
        agent_factory.create_my_deep_agent(**required)

    call = context_manager.call_args.kwargs
    assert call["owner"] is None
    assert call["control_state_port"] is None
    assert call["mutation_intent_port"] is None
