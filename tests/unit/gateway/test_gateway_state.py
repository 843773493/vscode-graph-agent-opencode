from __future__ import annotations

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget


def test_gateway_state_keeps_config_in_control_database(tmp_path):
    store = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        store.set_config(
            config_key="gateway",
            config_version=3,
            payload={"history_loading": {"initial_turns": 1}},
        )
        record = store.get_config("gateway")
        assert record is not None
        assert record.config_version == 3
        assert record.payload == {"history_loading": {"initial_turns": 1}}
        assert store.diagnostics().path.endswith("gateway.sqlite")
    finally:
        store.close()


def test_gateway_registry_uses_sqlite_without_mixing_session_indexes(tmp_path):
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        registry = GatewayWorkspaceRegistry(
            storage_path=tmp_path / "workspaces.json",
            state_store=state,
        )
        registry.upsert(
            WorkspaceTarget(
                workspace_id="workspace-a",
                name="A",
                root_path=str(tmp_path / "workspace-a"),
                backend_url="http://127.0.0.1:8010",
                connection_kind="local",
            )
        )
        restored = GatewayWorkspaceRegistry(
            storage_path=tmp_path / "workspaces.json",
            state_store=state,
        )
        assert [target.workspace_id for target in restored.targets()] == ["workspace-a"]
        assert state.diagnostics().path.endswith("gateway.sqlite")
        assert not (tmp_path / "workspace-a" / "rollout" / "index.sqlite").exists()
    finally:
        state.close()
