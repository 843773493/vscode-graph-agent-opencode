"""GraphBinding authority/read path wiring tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agents.graph_binding import (
    DEEP_AGENT_GRAPH_BINDING,
    GraphBinding,
    GraphBindingOwnerKey,
    GraphBindingStorePort,
    GraphBindingUnavailableError,
    GraphFactoryRegistry,
    resolve_graph_binding,
)
from app.runtime.agent_runtime import assemble_agent
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.orchestration.thread_residency import ThreadResidencyTracker


def test_runtime_selects_factory_from_injected_graph_binding_store() -> None:
    config_service = MagicMock()
    config_service.resolve_agent_id.return_value = "default"
    config_service.get_agent_tool_config.return_value = {"custom": []}
    config_service.resolve_agent_confirmation_tool_names.return_value = set()
    dependency_provider = MagicMock()
    dependency_provider.get_checkpointer.return_value = MagicMock()
    store = MagicMock(spec=GraphBindingStorePort)
    resource_registry = ResourceRegistry()
    resource_activation = MagicMock()
    skill_catalog = MagicMock()

    store.load_graph_binding.return_value = DEEP_AGENT_GRAPH_BINDING
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
            mcp_tools=[],
            resource_registry=resource_registry,
            resource_activation=resource_activation,
            skill_catalog=skill_catalog,
            graph_binding_store=store,
        )

    assert store.load_graph_binding.call_args.args == (GraphBindingOwnerKey("session_test", "thread_test"),)
    assert resolve_factory.call_args.args == (DEEP_AGENT_GRAPH_BINDING,)
    assert create_runtime.call_args.kwargs["resource_registry"] is resource_registry
    assert create_runtime.call_args.kwargs["resource_activation"] is resource_activation
    assert create_runtime.call_args.kwargs["skill_catalog"] is skill_catalog


def test_tool_catalog_uses_builtin_factory_without_persisted_owner() -> None:
    config_service = MagicMock()
    config_service.resolve_agent_id.return_value = "default"
    config_service.get_agent_tool_config.return_value = {"custom": []}
    config_service.resolve_agent_confirmation_tool_names.return_value = set()
    dependency_provider = MagicMock()
    dependency_provider.get_checkpointer.return_value = MagicMock()
    resource_registry = ResourceRegistry()
    resource_activation = MagicMock()
    skill_catalog = MagicMock()

    store = MagicMock(spec=GraphBindingStorePort)
    store.load_graph_binding.return_value = None
    with patch(
        "app.agents.graph_binding.GRAPH_FACTORY_REGISTRY.resolve"
    ) as resolve_factory:
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
            mcp_tools=[],
            resource_registry=resource_registry,
            resource_activation=resource_activation,
            skill_catalog=skill_catalog,
            graph_binding_store=store,
            assembly_mode="tool_catalog",
        )

    assert create_runtime.call_args.kwargs["assembly_mode"] == "tool_catalog"
    assert create_runtime.call_args.kwargs["model"] is None
    assert resolve_factory.call_args.args == (DEEP_AGENT_GRAPH_BINDING,)
    assert create_runtime.call_args.kwargs["resource_registry"] is resource_registry
    assert create_runtime.call_args.kwargs["resource_activation"] is resource_activation
    assert create_runtime.call_args.kwargs["skill_catalog"] is skill_catalog


def test_execution_service_passes_store_to_runtime() -> None:
    from app.services.orchestration.agent_execution_service import AgentExecutionService

    store = MagicMock(spec=GraphBindingStorePort)
    resource_registry = ResourceRegistry()
    resource_activation = MagicMock()
    skill_catalog = MagicMock()
    service = AgentExecutionService(
        config_service=MagicMock(),
        background_task_registry=MagicMock(),
        background_mailbox=MagicMock(),
        job_event_bus=MagicMock(),
        dependency_provider=MagicMock(),
        session_changes_service=MagicMock(),
        tool_selection_store=MagicMock(),
        message_stream_store=MagicMock(),
        workspace_id="00000000-0000-4000-8000-000000000001",
        workspace_root=Path("/workspace"),
        session_catalog=MagicMock(),
        graph_binding_store=store,
        residency_tracker=ThreadResidencyTracker(),
        resource_activation_factory=MagicMock(),
        team_resource_subscriber=MagicMock(),
    )

    with patch(
        "app.services.orchestration.agent_execution_service.assemble_agent"
    ) as build_runtime:
        service._assemble_agent(
            session_id="session_test",
            thread_id="thread_test",
            agent_id="default",
            execution_overrides={},
            model_visibility_overrides={},
            preferred_model=None,
            include_team_tools=False,
            mcp_tools=[],
            resource_registry=resource_registry,
            resource_activation=resource_activation,
            skill_catalog=skill_catalog,
        )

    assert build_runtime.call_args.kwargs["graph_binding_store"] is store
    assert build_runtime.call_args.kwargs["thread_id"] == "thread_test"
    assert build_runtime.call_args.kwargs["resource_registry"] is resource_registry
    assert build_runtime.call_args.kwargs["resource_activation"] is resource_activation
    assert build_runtime.call_args.kwargs["skill_catalog"] is skill_catalog


def _owner() -> GraphBindingOwnerKey:
    return GraphBindingOwnerKey(
        session_id="ses_019c58e6e9f5713785f99a3f3e5768d8",
        thread_id="thr_019c58e6e9f5713785f99a3f3e5768d8",
    )


def test_resolve_graph_binding_reads_and_resolves_exact_selector() -> None:
    store = MagicMock(spec=GraphBindingStorePort)
    registry = GraphFactoryRegistry()
    builder = lambda: object()
    registry.register(DEEP_AGENT_GRAPH_BINDING, builder)
    store.load_graph_binding.return_value = DEEP_AGENT_GRAPH_BINDING

    resolved = resolve_graph_binding(
        store=store,
        owner=_owner(),
        registry=registry,
    )

    assert resolved is DEEP_AGENT_GRAPH_BINDING
    assert store.load_graph_binding.call_args.args == (_owner(),)


def test_resolve_graph_binding_fails_when_catalog_selector_is_missing() -> None:
    store = MagicMock(spec=GraphBindingStorePort)
    store.load_graph_binding.return_value = None

    with pytest.raises(GraphBindingUnavailableError, match="graph_binding_unavailable"):
        resolve_graph_binding(store=store, owner=_owner())


@pytest.mark.parametrize(
    "field_name",
    ["graph_id", "graph_revision", "graph_schema_hash", "capability_profile_hash"],
)
def test_resolve_graph_binding_rejects_registry_drift_without_latest_fallback(
    field_name: str,
) -> None:
    registry = GraphFactoryRegistry()
    registry.register(DEEP_AGENT_GRAPH_BINDING, lambda: object())
    changes: dict[str, object] = {
        "graph_id": "deep-agent-legacy",
        "graph_revision": DEEP_AGENT_GRAPH_BINDING.graph_revision + 1,
        "graph_schema_hash": "sha256:" + "0" * 64,
        "capability_profile_hash": "sha256:" + "1" * 64,
    }
    selector = DEEP_AGENT_GRAPH_BINDING
    tampered = GraphBinding(
        graph_id=(
            changes["graph_id"] if field_name == "graph_id" else selector.graph_id
        ),
        graph_revision=(
            changes["graph_revision"]
            if field_name == "graph_revision"
            else selector.graph_revision
        ),
        graph_schema_hash=(
            changes["graph_schema_hash"]
            if field_name == "graph_schema_hash"
            else selector.graph_schema_hash
        ),
        capability_profile_hash=(
            changes["capability_profile_hash"]
            if field_name == "capability_profile_hash"
            else selector.capability_profile_hash
        ),
    )
    store = MagicMock(spec=GraphBindingStorePort)
    store.load_graph_binding.return_value = tampered

    with pytest.raises(GraphBindingUnavailableError, match="graph_binding_unavailable"):
        resolve_graph_binding(
            store=store,
            owner=_owner(),
            registry=registry,
        )
