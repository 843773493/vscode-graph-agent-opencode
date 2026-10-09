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
from app.runtime.agent_runtime import build_session_agent_runtime
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.orchestration.thread_residency import ThreadResidencyTracker


def test_runtime_layer_passes_graph_binding_store_through() -> None:
    config_service = MagicMock()
    config_service.resolve_agent_id.return_value = "default"
    dependency_provider = MagicMock()
    dependency_provider.get_checkpointer.return_value = MagicMock()
    store = MagicMock(spec=GraphBindingStorePort)

    with patch(
        "app.runtime.agent_runtime.create_runtime_deep_agent_for_session"
    ) as create_runtime:
        build_session_agent_runtime(
            session_id="session_test",
            thread_id="thread_test",
            agent_id="default",
            tool_selection_reader=MagicMock(),
            workspace_root=Path("/workspace"),
            config_service=config_service,
            background_task_registry=MagicMock(),
            background_message_bus=MagicMock(),
            job_event_bus=MagicMock(),
            dependency_provider=dependency_provider,
            mcp_tools=[],
            resource_registry=ResourceRegistry(),
            resource_activation=MagicMock(),
            graph_binding_store=store,
        )

    assert create_runtime.call_args.kwargs["graph_binding_store"] is store


def test_runtime_layer_passes_explicit_store_and_synthetic_owner_policy() -> None:
    config_service = MagicMock()
    config_service.resolve_agent_id.return_value = "default"
    dependency_provider = MagicMock()
    dependency_provider.get_checkpointer.return_value = MagicMock()

    with patch(
        "app.runtime.agent_runtime.create_runtime_deep_agent_for_session"
    ) as create_runtime:
        build_session_agent_runtime(
            session_id="session_test",
            thread_id="thread_test",
            agent_id="default",
            tool_selection_reader=MagicMock(),
            workspace_root=Path("/workspace"),
            config_service=config_service,
            background_task_registry=MagicMock(),
            background_message_bus=MagicMock(),
            job_event_bus=MagicMock(),
            dependency_provider=dependency_provider,
            mcp_tools=[],
            resource_registry=ResourceRegistry(),
            resource_activation=MagicMock(),
            graph_binding_store=MagicMock(spec=GraphBindingStorePort),
            owns_thread=False,
        )

    assert create_runtime.call_args.kwargs["owns_thread"] is False


def test_execution_service_passes_store_to_runtime() -> None:
    from app.services.orchestration.agent_execution_service import AgentExecutionService

    store = MagicMock(spec=GraphBindingStorePort)
    service = AgentExecutionService(
        config_service=MagicMock(),
        background_task_registry=MagicMock(),
        background_message_bus=MagicMock(),
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
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as build_runtime:
        service._build_agent(
            session_id="session_test",
            thread_id="thread_test",
            agent_id="default",
            execution_overrides={},
            model_visibility_overrides={},
            preferred_provider_id=None,
            include_team_tools=False,
            mcp_tools=[],
            resource_registry=ResourceRegistry(),
            resource_activation=MagicMock(),
        )

    assert build_runtime.call_args.kwargs["graph_binding_store"] is store
    assert build_runtime.call_args.kwargs["thread_id"] == "thread_test"


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
