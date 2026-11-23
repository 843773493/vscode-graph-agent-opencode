from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from boxteam_plugin_sdk.lifecycle import LifetimeScope

from app.agents.graph_binding import DEEP_AGENT_GRAPH_BINDING
from app.runtime.agent_runtime import assemble_agent, prepare_agent_model


def test_assemble_agent_injects_job_service() -> None:
    config_service = MagicMock()
    config_service.resolve_agent_id.return_value = "default"
    config_service.get_agent_tool_config.return_value = {"custom": []}
    config_service.resolve_agent_confirmation_tool_names.return_value = set()
    dependency_provider = MagicMock()
    job_service = MagicMock()
    dependency_provider.get_job_service.return_value = job_service
    dependency_provider.get_checkpointer.return_value = MagicMock()
    mcp_tools = [MagicMock()]
    visibility_overrides = {"write_file": False}
    dependency_provider.get_mcp_tools.return_value = mcp_tools
    resource_registry = MagicMock()
    resource_activation = MagicMock()
    skill_catalog = MagicMock()

    graph_binding_store = MagicMock()
    graph_binding_store.load_graph_binding.return_value = DEEP_AGENT_GRAPH_BINDING
    with (
        patch("app.runtime.agent_runtime.prepare_agent_model"),
        patch("app.agents.graph_binding.GRAPH_FACTORY_REGISTRY.resolve") as resolve_factory,
    ):
        create_runtime = resolve_factory.return_value
        assemble_agent(
            session_id="session_test",
            thread_id="thread_test",
            agent_id="default",
            tool_selection_reader=MagicMock(),
            workspace_root=Path("/workspace"),
            config_service=config_service,
            background_task_registry=MagicMock(),
            background_mailbox=MagicMock(),
            job_event_bus=MagicMock(),
            dependency_provider=dependency_provider,
            mcp_tools=mcp_tools,
            graph_binding_store=graph_binding_store,
            model_visibility_overrides=visibility_overrides,
            resource_registry=resource_registry,
            resource_activation=resource_activation,
            skill_catalog=skill_catalog,
        )

    assert create_runtime.call_args.kwargs["job_service"] is job_service
    assert create_runtime.call_args.kwargs["mcp_tools"] is mcp_tools
    assert create_runtime.call_args.kwargs["resource_registry"] is resource_registry
    assert create_runtime.call_args.kwargs["resource_activation"] is resource_activation
    assert create_runtime.call_args.kwargs["resource_navigation"] is dependency_provider.get_resource_navigation.return_value
    assert create_runtime.call_args.kwargs["text_mutations"] is dependency_provider.get_text_mutations.return_value
    assert (
        create_runtime.call_args.kwargs["node_debug_service"]
        is dependency_provider.get_node_debug_service.return_value
    )
    assert create_runtime.call_args.kwargs["skill_catalog"] is skill_catalog
    dependency_provider.get_mcp_tools.assert_not_called()
    assert (
        create_runtime.call_args.kwargs["model_visibility_overrides"]
        is visibility_overrides
    )
    assert create_runtime.call_args.kwargs["workspace_root"] == Path("/workspace")


@pytest.mark.asyncio
async def test_thread_lifetime_closes_all_candidate_model_pools() -> None:
    """真实模型工厂与 SDK 池归现有 scope，包含尚未被路由选中的回退模型。"""
    config_service = MagicMock()
    config_service.get_revision.return_value = "config:lifetime"
    config_service.get_agent_runtime_config.return_value = {
        "system_prompt": "test", "runtime_identity_description": "test",
        "models": [{
            "provider_id": "test", "model_id": name, "model": "test-model",
            "endpoint": "http://model.invalid/v1", "api_key": "test-key", "custom_llm_provider": "openai",
            "api_mode": {"protocol": "chat_completions", "model_info": {}, "supports_reasoning": {}},
        } for name in ("primary", "fallback")],
    }
    scope = LifetimeScope("thread-models")
    setup = prepare_agent_model("default", config_service, lifetime_scope=scope)
    pools = []
    try:
        for candidate in setup.routing_middleware._candidates:
            pools.append(candidate.model._provider_http_client(is_async=True)._client)
            pools.append(candidate.model._provider_http_client(is_async=False)._client)
        assert all(not pool.is_closed for pool in pools)
    finally:
        await scope.close()
    assert all(pool.is_closed for pool in pools)


async def test_candidate_binding_failure_keeps_created_model_in_scope(monkeypatch) -> None:
    config = MagicMock()
    config.get_agent_runtime_config.return_value = {
        "models": [{"provider_id": "test", "model_id": "primary"}],
    }
    model = MagicMock(aclose=AsyncMock())
    failure = ValueError("候选能力解析失败")

    def fail_candidate(**_kwargs):
        raise failure

    monkeypatch.setattr("app.runtime.provider_model.build_model_from_provider", lambda *_a, **_k: model)
    monkeypatch.setattr("app.agents.model_capability_routing.build_provider_model_candidate", fail_candidate)
    scope = LifetimeScope("candidate-binding-failure")
    try:
        with pytest.raises(ValueError) as observed:
            prepare_agent_model("default", config, lifetime_scope=scope)
        assert observed.value is failure
    finally:
        await scope.close()
    model.aclose.assert_awaited_once()
