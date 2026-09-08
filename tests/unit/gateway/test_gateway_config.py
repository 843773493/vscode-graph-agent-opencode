from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.gateway.config as gateway_config_module
from app.gateway.config import (
    REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS,
    GatewayConfig,
    GatewayConfigReloadService,
    load_gateway_config,
    resolve_gateway_path,
    rollback_gateway_connection_id_migration,
)
from app.gateway.control.catalog_search import GatewaySessionCatalogSearchService
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.generators import SessionGeneratorStore
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.control.scheduler import SessionGeneratorScheduler
from app.gateway.main import (
    _apply_gateway_runtime_config,
    _gateway_pending_consumer_health_digests,
    _gateway_runtime_consumer_stages,
    _should_preserve_gateway_generation_for_handoff,
    gateway_config_sources,
)
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.services.infrastructure.config.state import ConfigConflictError


def _write_gateway_config(
    config_root: Path,
    workspaces: list[dict[str, object]],
) -> tuple[Path, Path]:
    config_root.mkdir(parents=True, exist_ok=True)
    config_path = config_root / "gateway.jsonc"
    schema_path = config_root / "gateway_schema.jsonc"
    config_path.write_text(
        json.dumps(
            {
                "$schema": "./gateway_schema.jsonc",
                "config_version": 1,
                "workspaces": workspaces,
            }
        ),
        encoding="utf-8",
    )
    schema_path.write_bytes(Path("configs/gateway_schema.jsonc").read_bytes())
    return config_path, schema_path


def _gateway_consumer_health_digests(value: str = "a") -> dict[str, str]:
    return {
        consumer_id: value * 64
        for consumer_id in sorted(REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS)
    }


@pytest.mark.asyncio
async def test_gateway_runtime_apply_rolls_back_consumers_on_partial_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    catalog = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=SimpleNamespace(),  # 仅测试配置提交，不启动目录请求
        cache_dir=tmp_path / "indexes",
        navigation_store=WorkspaceNavigationStore(
            storage_path=tmp_path / "navigation.json"
        ),
    )
    scheduler = SessionGeneratorScheduler(
        store=SessionGeneratorStore(root=tmp_path / "generators"),
        coordinator=SimpleNamespace(),
    )
    initial = GatewayConfig(
        session_catalog_refresh_interval_seconds=30,
        session_catalog_max_concurrency=8,
        session_catalog_request_timeout_seconds=30,
        session_generator_poll_interval_seconds=1,
    )
    candidate = GatewayConfig(
        session_catalog_refresh_interval_seconds=5,
        session_catalog_max_concurrency=2,
        session_catalog_request_timeout_seconds=6,
        session_generator_poll_interval_seconds=2,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            gateway_config=initial,
            session_catalog_search_service=catalog,
            session_generator_scheduler=scheduler,
        )
    )

    def fail_scheduler_update(self, *, poll_interval_seconds: float) -> None:
        del self, poll_interval_seconds
        raise RuntimeError("模拟 scheduler prepare/apply 失败")

    monkeypatch.setattr(
        SessionGeneratorScheduler,
        "update_runtime_config",
        fail_scheduler_update,
    )

    with pytest.raises(RuntimeError, match="模拟 scheduler prepare/apply 失败"):
        await _apply_gateway_runtime_config(app, candidate, initial)

    assert app.state.gateway_config is initial
    assert catalog._refresh_interval_seconds == 30
    assert catalog._max_concurrency == 8
    assert catalog._request_timeout_seconds == 30


def test_gateway_runtime_consumer_generation_is_unique_for_same_config(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    catalog = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=SimpleNamespace(),
        cache_dir=tmp_path / "indexes",
        navigation_store=WorkspaceNavigationStore(
            storage_path=tmp_path / "navigation.json"
        ),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_catalog_search_service=catalog,
            session_generator_scheduler=None,
            workspace_runtime_controller=None,
        )
    )
    config = GatewayConfig(revision="same-digest")

    first = _gateway_runtime_consumer_stages(app, config, config)
    second = _gateway_runtime_consumer_stages(app, config, config)

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].generation != second[0].generation
    assert registry.registry_revision == 0
    registry.close()


def test_gateway_runtime_consumer_health_proof_carries_fencing_digest(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    catalog = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=SimpleNamespace(),
        cache_dir=tmp_path / "indexes",
        navigation_store=WorkspaceNavigationStore(
            storage_path=tmp_path / "navigation.json"
        ),
    )
    catalog._task = SimpleNamespace(done=lambda: False)  # type: ignore[assignment]
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_catalog_search_service=catalog,
            session_generator_scheduler=None,
            workspace_runtime_controller=None,
        )
    )

    stages = _gateway_runtime_consumer_stages(
        app,
        GatewayConfig(revision="fenced-digest"),
        GatewayConfig(revision="previous"),
        fencing_token="fence-token",
    )

    assert len(stages) == 1
    proof = stages[0].health()
    assert proof.fencing_token_digest is not None
    assert proof.fencing_token_digest == stages[0].fencing_token_digest
    registry.close()


def test_gateway_pending_proof_collects_all_runtime_consumer_digests(
    tmp_path: Path,
) -> None:
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    catalog = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=SimpleNamespace(),
        cache_dir=tmp_path / "indexes",
        navigation_store=WorkspaceNavigationStore(
            storage_path=tmp_path / "navigation.json"
        ),
    )
    catalog._task = SimpleNamespace(done=lambda: False)  # type: ignore[assignment]
    scheduler = SessionGeneratorScheduler(
        store=SessionGeneratorStore(root=tmp_path / "generators"),
        coordinator=SimpleNamespace(),
    )
    scheduler._task = SimpleNamespace(done=lambda: False)  # type: ignore[assignment]
    controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
    )
    port_forward_manager = SshPortForwardManager(
        registry=registry,
        storage_path=tmp_path / "port-forwards.json",
        log_dir=tmp_path / "logs",
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            registry=registry,
            session_catalog_search_service=catalog,
            session_generator_scheduler=scheduler,
            workspace_runtime_controller=controller,
            port_forward_manager=port_forward_manager,
        )
    )

    digests = _gateway_pending_consumer_health_digests(
        app,
        generation="gateway-generation",
        fencing_token_digest="f" * 64,
    )

    assert set(digests) == {
        "catalog-generator-scheduler",
        "health-controller",
        "registry-batch",
        "ssh-tunnel-proxy",
        "workspace-process",
        "remote-projection",
    }
    assert all(len(digest) == 64 for digest in digests.values())
    registry.close()


def test_load_gateway_config_accepts_remote_gateway(tmp_path: Path) -> None:
    config_path, schema_path = _write_gateway_config(
        tmp_path,
        [
            {
                "kind": "remote_gateway",
                "host": "remote.example.com",
                "username": "developer",
                "private_key_path": "keys/id_ed25519",
                "ssh_config_host": "developer-server",
                "remote_pair_command": "boxteam gateway issue-federation-token",
                "remote_gateway_port": 9014,
            }
        ],
    )
    result = load_gateway_config(config_path=config_path, schema_path=schema_path)

    assert len(result.workspaces) == 1
    workspace = result.workspaces[0]
    assert workspace.port == 22
    assert workspace.ssh_config_host == "developer-server"
    assert workspace.remote_pair_command == "boxteam gateway issue-federation-token"
    assert workspace.remote_gateway_port == 9014
    assert workspace.activate is False
    assert result.default_workspace_skill_groups == (
        "browser-control",
        "gateway-context",
        "web-search-fetch",
        "debugging",
    )


def test_gateway_shutdown_preserves_old_generation_for_pending_handoff(
    tmp_path: Path,
) -> None:
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        state.create_pending_config_candidate(
            config_domain="gateway",
            candidate_id="candidate-handoff",
            idempotency_key="reload:handoff",
            payload={"config_version": 2, "workspaces": []},
            source_baseline={},
            candidate_digest="candidate-handoff-digest",
            effective_digest="candidate-handoff-digest",
            target_generation="gateway-new",
            fencing_token=None,
            state="candidate_validated",
        )
        state.update_pending_config_candidate_state(
            config_domain="gateway",
            candidate_id="candidate-handoff",
            expected_state="candidate_validated",
            state="pending_restart",
        )
        state.request_gateway_restart(
            candidate_ref="candidate-ref-handoff",
            candidate_id="candidate-handoff",
            base_active_revision=None,
            old_generation="gateway-old",
            target_generation="gateway-new",
            requested_by="test",
        )
        reload_service = SimpleNamespace(
            status=lambda: SimpleNamespace(
                candidate_ref="candidate-ref-handoff",
                state="pending_restart",
            )
        )

        assert _should_preserve_gateway_generation_for_handoff(
            gateway_state=state,
            gateway_config_reload=reload_service,
            startup_candidate_ref=None,
            generation_id="gateway-old",
        )
        assert not _should_preserve_gateway_generation_for_handoff(
            gateway_state=state,
            gateway_config_reload=reload_service,
            startup_candidate_ref="candidate-ref-handoff",
            generation_id="gateway-old",
        )
        assert not _should_preserve_gateway_generation_for_handoff(
            gateway_state=state,
            gateway_config_reload=reload_service,
            startup_candidate_ref=None,
            generation_id="another-generation",
        )
    finally:
        state.close()


@pytest.mark.parametrize(
    "workspace",
    [
        {
            "kind": "remote_gateway",
            "host": "remote.example.com",
            "username": "developer",
            "private_key_path": "keys/id_ed25519",
            "remote_workspace_path": "/workspace/project",
        },
        {
            "kind": "remote_gateway",
            "host": "remote.example.com",
            "username": "developer",
            "private_key_path": "keys/id_ed25519",
            "port": 70000,
        },
    ],
)
def test_load_gateway_config_rejects_schema_violation(
    tmp_path: Path,
    workspace: dict[str, object],
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [workspace])

    with pytest.raises(ValueError, match="配置验证失败"):
        load_gateway_config(config_path=config_path, schema_path=schema_path)


def test_load_gateway_config_skips_disabled_workspace(tmp_path: Path) -> None:
    config_path, schema_path = _write_gateway_config(
        tmp_path,
        [
            {
                "enabled": False,
                "kind": "remote_gateway",
                "host": "127.0.0.1",
                "username": "boxteam",
                "private_key_path": "~/.ssh/boxteam_gateway_e2e_ed25519",
            }
        ],
    )
    assert (
        load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
        ).workspaces
        == ()
    )


def test_gateway_history_loading_config_is_nested_and_uses_anchor_window(
    tmp_path: Path,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    config_path.write_text(
        json.dumps(
            {
                "$schema": "./gateway_schema.jsonc",
                "config_version": 1,
                "workspaces": [],
                "features": {
                    "session_history": {
                        "loading": {
                            "progressive": {
                                "initial": {
                                    "turns": 1,
                                    "include": [
                                        "user",
                                        "tool_summary",
                                        "final_response",
                                    ],
                                },
                                "anchor": {
                                    "before_turns": 2,
                                    "after_turns": 5,
                                    "include": ["user", "final_response"],
                                },
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = load_gateway_config(config_path=config_path, schema_path=schema_path)

    assert result.history_loading.initial_turns == 1
    assert result.history_loading.anchor_before_turns == 2
    assert result.history_loading.anchor_after_turns == 5
    assert result.history_loading.anchor_limit("before") == 2
    assert result.history_loading.anchor_limit("after") == 5


def test_gateway_history_loading_defaults_use_five_initial_and_three_sided_turns(
    tmp_path: Path,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])

    result = load_gateway_config(config_path=config_path, schema_path=schema_path)

    assert result.history_loading.initial_turns == 5
    assert result.history_loading.anchor_before_turns == 3
    assert result.history_loading.anchor_after_turns == 3


def test_load_gateway_config_merges_local_override_and_records_sources(
    tmp_path: Path,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    local_config_path = tmp_path / "gateway_local.jsonc"
    local_config_path.write_text(
        json.dumps(
            {
                "ui": {"theme": {"default_theme_id": "green"}},
            }
        ),
        encoding="utf-8",
    )

    result = load_gateway_config(
        config_path=config_path,
        schema_path=schema_path,
    )

    assert result.default_theme_id == "green"
    assert result.source_paths == (
        Path("configs/gateway_inline.jsonc").resolve(),
        config_path,
        local_config_path,
    )
    assert [source.layer for source in result.source_details] == [
        "inline",
        "user",
        "user_local",
    ]
    assert result.source_details[-1].loaded is True


@pytest.mark.asyncio
async def test_gateway_config_sources_endpoint_exposes_effective_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    local_config_path = tmp_path / "gateway_local.jsonc"
    local_config_path.write_text(
        json.dumps({"ui": {"theme": {"default_theme_id": "blue"}}}),
        encoding="utf-8",
    )
    config = load_gateway_config(config_path=config_path, schema_path=schema_path)
    monkeypatch.setattr("app.gateway.main.load_gateway_config", lambda: config)

    response = await gateway_config_sources(
        _="gateway-token",
        request_id="req-gateway-config-sources",
    )

    assert response.request_id == "req-gateway-config-sources"
    assert response.data is not None
    assert response.data.schema_path == str(schema_path)
    assert [source.layer for source in response.data.sources] == [
        "inline",
        "user",
        "user_local",
    ]
    assert response.data.sources[1].loaded is True
    assert response.data.policy_manifest


def test_gateway_loader_does_not_read_workspace_configuration(tmp_path: Path) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path / "home", [])
    workspace_config = tmp_path / "workspace" / ".boxteam" / "workspace.jsonc"
    workspace_config.parent.mkdir(parents=True)
    workspace_config.write_text(
        '{"gateway": {"workspaces": "invalid"}}\n',
        encoding="utf-8",
    )

    assert (
        load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
        ).workspaces
        == ()
    )


def test_resolve_gateway_relative_path_uses_installed_config_directory(
    tmp_path: Path,
) -> None:
    assert (
        resolve_gateway_path(
            "keys/gateway_ed25519",
            config_root=tmp_path,
        )
        == (tmp_path / "keys" / "gateway_ed25519").resolve()
    )


def test_gateway_config_migrates_mutable_json_layers_to_sqlite(tmp_path: Path) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    local_config_path = tmp_path / "gateway_local.jsonc"
    local_config_path.write_text(
        json.dumps({"ui": {"theme": {"default_theme_id": "blue"}}}),
        encoding="utf-8",
    )
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        config = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            local_config_path=local_config_path,
            state_store=state,
        )
        assert config.default_theme_id == "blue"
        assert state.get_config("gateway_mutable_override") is not None
        assert state.get_config("gateway_local_mutable_override") is not None
        assert config_path.with_name("gateway.jsonc.migrated.bak").is_file()
        assert local_config_path.with_name("gateway_local.jsonc.migrated.bak").is_file()
    finally:
        state.close()


def test_gateway_connection_id_migration_is_comment_preserving_and_idempotent(
    tmp_path: Path,
) -> None:
    config_path, schema_path = _write_gateway_config(
        tmp_path,
        [
            {
                "kind": "remote_gateway",
                "host": "remote.example.com",
                "username": "developer",
                "private_key_path": "keys/id_ed25519",
            }
        ],
    )
    original = config_path.read_bytes()
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            '"config_version": 1',
            '// 保留用户注释\n  "config_version": 1',
        ),
        encoding="utf-8",
    )
    original = config_path.read_bytes()
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        migrated = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        assert migrated.workspaces[0].connection_id
        migrated_text = config_path.read_text(encoding="utf-8")
        assert "保留用户注释" in migrated_text
        assert '"config_version": 2' in migrated_text
        assert migrated.workspaces[0].connection_id in migrated_text
        record = state.get_config("gateway_connection_ids")
        assert record is not None
        assert record.payload["migration"]["state"] == "completed"
        backup_path = Path(record.payload["migration"]["backup_path"])
        assert backup_path.read_bytes() == original
        second = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        assert (
            second.workspaces[0].connection_id == migrated.workspaces[0].connection_id
        )
        assert config_path.read_bytes() == migrated_text.encode("utf-8")
    finally:
        state.close()


def test_gateway_connection_id_migration_can_restore_v1_backup(
    tmp_path: Path,
) -> None:
    config_path, schema_path = _write_gateway_config(
        tmp_path,
        [
            {
                "kind": "remote_gateway",
                "host": "remote.example.com",
                "username": "developer",
                "private_key_path": "keys/id_ed25519",
            }
        ],
    )
    original = config_path.read_bytes()
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        assert config_path.read_bytes() != original
        rollback_gateway_connection_id_migration(
            config_path=config_path,
            state_store=state,
        )
        assert config_path.read_bytes() == original
        record = state.get_config("gateway_connection_ids")
        assert record is not None
        assert record.payload["migration"]["state"] == "rolled_back"
    finally:
        state.close()


def test_gateway_connection_id_migration_recovers_after_file_write_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, schema_path = _write_gateway_config(
        tmp_path,
        [
            {
                "kind": "remote_gateway",
                "host": "remote.example.com",
                "username": "developer",
                "private_key_path": "keys/id_ed25519",
            }
        ],
    )
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    original_writer = gateway_config_module._atomic_write_gateway_jsonc

    def write_then_crash(path: Path, raw_bytes: bytes) -> None:
        original_writer(path, raw_bytes)
        raise OSError("模拟迁移写入后进程退出")

    monkeypatch.setattr(
        gateway_config_module,
        "_atomic_write_gateway_jsonc",
        write_then_crash,
    )
    try:
        with pytest.raises(OSError, match="写入后进程退出"):
            load_gateway_config(
                config_path=config_path,
                schema_path=schema_path,
                state_store=state,
            )
        planned = state.get_config("gateway_connection_ids")
        assert planned is not None
        assert planned.payload["migration"]["state"] == "planned"
        assert '"config_version": 2' in config_path.read_text(encoding="utf-8")

        monkeypatch.setattr(
            gateway_config_module,
            "_atomic_write_gateway_jsonc",
            original_writer,
        )
        restored = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        completed = state.get_config("gateway_connection_ids")
        assert completed is not None
        assert completed.payload["migration"]["state"] == "completed"
        assert restored.workspaces[0].connection_id
    finally:
        state.close()


def test_gateway_connection_id_migration_rejects_partial_source_layer(
    tmp_path: Path,
) -> None:
    config_path, _ = _write_gateway_config(
        tmp_path,
        [
            {
                "kind": "remote_gateway",
                "host": "remote-a.example.com",
                "username": "developer",
                "private_key_path": "keys/id_ed25519",
            },
            {
                "kind": "remote_gateway",
                "host": "remote-b.example.com",
                "username": "developer",
                "private_key_path": "keys/id_ed25519",
            },
        ],
    )
    original = config_path.read_bytes()
    raw_config = json.loads(original)
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        with pytest.raises(
            gateway_config_module.ConfigConflictError,
            match="partial source layer",
        ):
            gateway_config_module._migrate_gateway_connection_ids_in_source(
                raw_config=raw_config,
                config_path=config_path,
                state_store=state,
                connection_ids=("connection-one",),
            )
        assert config_path.read_bytes() == original
        assert state.get_config("gateway_connection_ids") is None
    finally:
        state.close()


def test_gateway_startup_without_ref_uses_persisted_active_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        initial = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        service = GatewayConfigReloadService(
            state_store=state,
            config=initial,
            config_path=config_path,
            local_config_path=tmp_path / "gateway_local.jsonc",
            schema_path=schema_path,
            gateway_id="gateway-test",
        )
        service.initialize_active_snapshot()
        config_path.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "workspaces": [],
                    "ui": {"theme": {"default_theme_id": "blue"}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.delenv("BOXTEAM_CONFIG_CANDIDATE_REF", raising=False)
        restored = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
            startup=True,
        )
        assert restored.revision == initial.revision
        assert restored.default_theme_id == initial.default_theme_id
    finally:
        state.close()


@pytest.mark.asyncio
async def test_gateway_pending_startup_loads_exact_candidate_and_promotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        initial = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        service = GatewayConfigReloadService(
            state_store=state,
            config=initial,
            config_path=config_path,
            local_config_path=tmp_path / "gateway_local.jsonc",
            schema_path=schema_path,
            gateway_id="gateway-test",
        )
        service.initialize_active_snapshot()
        state.record_gateway_runtime_generation(
            generation_id="gateway-bootstrap",
            process_id=100,
            loaded_source="active",
            candidate_id=None,
            active_revision=1,
            pending_revision=None,
            candidate_digest=None,
            effective_digest=initial.revision,
            secret_binding_digest="bootstrap-secret-digest",
            fencing_token=None,
            listener_state="serving",
            state="active",
        )
        config_path.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "workspaces": [],
                    "runtime": {
                        "gateway": {"process": {"health": {"poll_interval_seconds": 1}}}
                    },
                }
            ),
            encoding="utf-8",
        )
        await service.reload()
        pending_status = service.status()
        assert pending_status.candidate_ref is not None
        intent = state.get_gateway_restart_intent(
            candidate_ref=pending_status.candidate_ref
        )
        assert intent is not None
        monkeypatch.delenv("BOXTEAM_CONFIG_CANDIDATE_REF", raising=False)
        monkeypatch.delenv("BOXTEAM_CONFIG_GENERATION", raising=False)
        monkeypatch.delenv("BOXTEAM_CONFIG_FENCING_TOKEN", raising=False)
        crash_recovered = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
            startup=True,
        )
        assert crash_recovered.revision == initial.revision
        assert (
            state.get_pending_config_candidate(config_domain="gateway").state
            == "pending_restart"
        )
        monkeypatch.setenv("BOXTEAM_CONFIG_CANDIDATE_REF", pending_status.candidate_ref)
        monkeypatch.setenv("BOXTEAM_CONFIG_GENERATION", intent.target_generation)
        monkeypatch.setenv("BOXTEAM_CONFIG_FENCING_TOKEN", intent.fencing_token)
        restored = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
            startup=True,
        )
        assert restored.revision != initial.revision
        assert restored.payload["runtime"] != initial.payload.get("runtime")
        restarted_service = GatewayConfigReloadService(
            state_store=state,
            config=restored,
            config_path=config_path,
            local_config_path=tmp_path / "gateway_local.jsonc",
            schema_path=schema_path,
            gateway_id="gateway-test",
        )
        restarted_service.begin_pending_restart(
            candidate_ref=pending_status.candidate_ref
        )
        proof = restarted_service.build_pending_restart_health_proof(
            candidate_ref=pending_status.candidate_ref,
            generation=intent.target_generation,
            consumer_health_digests=_gateway_consumer_health_digests(),
        )
        assert proof["consumer_health_digests"] == _gateway_consumer_health_digests()
        pending = state.get_pending_config_candidate(config_domain="gateway")
        claim = state.get_config_apply_claim(config_domain="gateway")
        assert pending is not None
        assert claim is not None
        state.record_gateway_runtime_generation(
            generation_id=intent.target_generation,
            process_id=101,
            loaded_source="pending",
            candidate_id=pending.candidate_id,
            active_revision=(
                state.get_active_config_snapshot("gateway").active_revision
            ),
            pending_revision=pending.pending_revision,
            candidate_digest=pending.candidate_digest,
            effective_digest=pending.effective_digest,
            secret_binding_digest=proof["secret_binding_digest"],
            fencing_token=claim.fencing_token,
            listener_state="reserved",
            state="healthy",
            health_proof=proof,
        )
        state.update_gateway_runtime_generation(
            generation_id="gateway-bootstrap",
            expected_state="active",
            state="active",
            listener_state="draining",
        )
        with pytest.raises(ConfigConflictError, match="完整 consumer"):
            restarted_service.record_pending_restart_proof(
                candidate_ref=pending_status.candidate_ref,
                health_proof={
                    **proof,
                    "consumer_health_digests": {
                        "health-controller": "a" * 64,
                    },
                },
            )
        with pytest.raises(ConfigConflictError, match="health proof"):
            restarted_service.record_pending_restart_proof(
                candidate_ref=pending_status.candidate_ref,
                health_proof={
                    **proof,
                    "consumer_health_digests": {"health-controller": "b" * 64},
                },
            )
        promoted = restarted_service.record_pending_restart_proof(
            candidate_ref=pending_status.candidate_ref,
            health_proof=proof,
        )
        assert promoted.effective_digest == restored.revision
        assert (
            state.get_gateway_restart_intent(
                candidate_ref=pending_status.candidate_ref
            ).state
            == "active"
        )
        assert (
            state.get_pending_config_candidate(config_domain="gateway").state
            == "active"
        )
        promoted_status = restarted_service.status()
        assert promoted_status.state == "active"
        assert promoted_status.pending_revision is None
        assert promoted_status.candidate_id is None
        assert promoted_status.candidate_ref is None
        assert promoted_status.attempt_id is None
        assert promoted_status.apply_id is None
        promoted_generation = state.get_gateway_runtime_generation(
            generation_id=intent.target_generation
        )
        old_generation = state.get_gateway_runtime_generation(
            generation_id="gateway-bootstrap"
        )
        assert promoted_generation is not None
        assert old_generation is not None
        assert promoted_generation.state == "active"
        assert promoted_generation.listener_state == "serving"
        assert old_generation.state == "active"
        assert old_generation.listener_state == "draining"
    finally:
        state.close()


@pytest.mark.asyncio
async def test_gateway_config_reload_promotes_ui_candidate_and_emits_event(
    tmp_path: Path,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        initial = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        service = GatewayConfigReloadService(
            state_store=state,
            config=initial,
            config_path=config_path,
            local_config_path=tmp_path / "gateway_local.jsonc",
            schema_path=schema_path,
        )
        service.initialize_active_snapshot()
        config_path.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "workspaces": [],
                    "ui": {"theme": {"default_theme_id": "blue"}},
                }
            ),
            encoding="utf-8",
        )
        await service.reload()
        status = service.status()
        assert status.state == "active"
        assert status.restart_required is False
        assert status.revision != initial.revision
        assert status.pending_revision is None
        assert status.candidate_id is None
        assert status.candidate_ref is None
        assert status.attempt_id is None
        assert status.apply_id is None
        assert [event.result for event in service.list_events()] == ["applied"]
    finally:
        state.close()


@pytest.mark.asyncio
async def test_gateway_config_reload_compensates_runtime_apply_on_promotion_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    applied: list[tuple[str, str]] = []

    async def on_runtime_config(candidate, previous, fencing_token, fence_check):
        assert fencing_token
        fence_check()
        applied.append(("apply", candidate.revision))

        async def rollback() -> None:
            applied.append(("rollback", previous.revision))

        return rollback

    try:
        initial = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        service = GatewayConfigReloadService(
            state_store=state,
            config=initial,
            config_path=config_path,
            local_config_path=tmp_path / "gateway_local.jsonc",
            schema_path=schema_path,
            on_runtime_config=on_runtime_config,
        )
        service.initialize_active_snapshot()
        config_path.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "workspaces": [],
                    "ui": {"theme": {"default_theme_id": "blue"}},
                }
            ),
            encoding="utf-8",
        )

        def reject_promotion(*args, **kwargs):
            raise ConfigConflictError("模拟 promotion CAS 冲突")

        monkeypatch.setattr(state, "promote_active_config_snapshot", reject_promotion)
        with pytest.raises(ConfigConflictError, match="模拟 promotion CAS 冲突"):
            await service.reload()

        assert [item[0] for item in applied] == ["apply", "rollback"]
        assert applied[0][1] != initial.revision
        assert applied[1][1] == initial.revision
        active = state.get_active_config_snapshot("gateway")
        pending = state.get_pending_config_candidate(config_domain="gateway")
        assert active is not None
        assert pending is not None
        assert active.effective_digest == initial.revision
        assert pending.state == "conflict"
        assert pending.last_apply_id is not None
        journal = state.get_config_apply_journal(apply_id=pending.last_apply_id)
        assert journal is not None
        assert journal.state == "compensated"
        assert {
            (str(effect.get("resource")), str(effect.get("action")))
            for effect in journal.side_effects
        } == {
            ("gateway-runtime-config", "apply"),
            ("gateway-runtime-config", "rollback"),
        }
    finally:
        state.close()


@pytest.mark.asyncio
async def test_gateway_config_reload_keeps_old_active_for_runtime_pending(
    tmp_path: Path,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        initial = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        service = GatewayConfigReloadService(
            state_store=state,
            config=initial,
            config_path=config_path,
            local_config_path=tmp_path / "gateway_local.jsonc",
            schema_path=schema_path,
            gateway_id="gateway-test",
        )
        service.initialize_active_snapshot()
        config_path.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "workspaces": [],
                    "runtime": {
                        "gateway": {"process": {"health": {"poll_interval_seconds": 1}}}
                    },
                }
            ),
            encoding="utf-8",
        )
        await service.reload()
        status = service.status()
        assert status.state == "pending_restart"
        assert status.restart_required is True
        active = state.get_active_config_snapshot("gateway")
        pending = state.get_pending_config_candidate(config_domain="gateway")
        assert active is not None
        assert pending is not None
        assert active.effective_digest == initial.revision
        assert pending.state == "pending_restart"
        assert status.candidate_ref is not None
        restart_intent = state.get_gateway_restart_intent(
            candidate_ref=status.candidate_ref
        )
        assert restart_intent is not None
        assert restart_intent.candidate_id == pending.candidate_id
        assert (
            service.load_pending_candidate(
                candidate_ref=status.candidate_ref
            ).candidate_id
            == pending.candidate_id
        )
        assert [event.result for event in service.list_events()] == ["restart_required"]
        failed_status = service.record_pending_restart_failure(
            candidate_ref=status.candidate_ref,
            target_generation=restart_intent.target_generation,
            fencing_token=restart_intent.fencing_token,
            error="新 Gateway generation 健康检查失败",
        )
        assert failed_status.state == "recovery_required"
        failed_intent = state.get_gateway_restart_intent(
            candidate_ref=status.candidate_ref
        )
        assert failed_intent is not None
        assert failed_intent.state == "recovery_required"
        retried_status = service.retry_pending_restart(
            candidate_ref=status.candidate_ref
        )
        assert retried_status.state == "pending_restart"
        retried_intent = state.get_gateway_restart_intent(
            candidate_ref=status.candidate_ref
        )
        assert retried_intent is not None
        assert retried_intent.state == "pending"
        assert retried_intent.fencing_token != failed_intent.fencing_token
    finally:
        state.close()


@pytest.mark.asyncio
async def test_gateway_pending_restart_failure_rejects_stale_startup_contract(
    tmp_path: Path,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        initial = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        service = GatewayConfigReloadService(
            state_store=state,
            config=initial,
            config_path=config_path,
            local_config_path=tmp_path / "gateway_local.jsonc",
            schema_path=schema_path,
            gateway_id="gateway-test",
        )
        service.initialize_active_snapshot()
        config_path.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "workspaces": [],
                    "runtime": {
                        "gateway": {"process": {"health": {"poll_interval_seconds": 1}}}
                    },
                }
            ),
            encoding="utf-8",
        )
        await service.reload()
        candidate_ref = service.status().candidate_ref
        assert candidate_ref is not None
        intent = state.get_gateway_restart_intent(candidate_ref=candidate_ref)
        assert intent is not None
        pending_before = state.get_pending_config_candidate(config_domain="gateway")
        assert pending_before is not None
        event_count = len(service.list_events())

        for generation, fencing_token in (
            (f"stale-{intent.target_generation}", intent.fencing_token),
            (intent.target_generation, "stale-fencing-token"),
        ):
            with pytest.raises(ConfigConflictError):
                service.record_pending_restart_failure(
                    candidate_ref=candidate_ref,
                    target_generation=generation,
                    fencing_token=fencing_token,
                    error="迟到的 Gateway generation 失败回报",
                )

        unchanged_intent = state.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        unchanged_pending = state.get_pending_config_candidate(
            config_domain="gateway",
            candidate_id=intent.candidate_id,
        )
        assert unchanged_intent is not None
        assert unchanged_intent.state == "pending"
        assert unchanged_intent.last_error is None
        assert unchanged_pending is not None
        assert unchanged_pending.state == "pending_restart"
        assert unchanged_pending.last_error == pending_before.last_error
        assert len(service.list_events()) == event_count

        failed_status = service.record_pending_restart_failure(
            candidate_ref=candidate_ref,
            target_generation=intent.target_generation,
            fencing_token=intent.fencing_token,
            error="匹配契约的 Gateway generation 失败回报",
        )
        assert failed_status.state == "recovery_required"
        recovery_event = service.list_events()[-1]
        assert recovery_event.result == "recovery_required"
        assert recovery_event.idempotency_key == pending_before.idempotency_key
    finally:
        state.close()


@pytest.mark.asyncio
async def test_gateway_recovery_pending_requires_matching_proof_before_resolve(
    tmp_path: Path,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        initial = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        service = GatewayConfigReloadService(
            state_store=state,
            config=initial,
            config_path=config_path,
            local_config_path=tmp_path / "gateway_local.jsonc",
            schema_path=schema_path,
            gateway_id="gateway-test",
        )
        service.initialize_active_snapshot()
        state.record_gateway_runtime_generation(
            generation_id="gateway-bootstrap",
            process_id=100,
            loaded_source="active",
            candidate_id=None,
            active_revision=1,
            pending_revision=None,
            candidate_digest=None,
            effective_digest=initial.revision,
            secret_binding_digest="bootstrap-secret-digest",
            fencing_token=None,
            listener_state="serving",
            state="active",
        )
        config_path.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "workspaces": [],
                    "runtime": {
                        "gateway": {"process": {"health": {"poll_interval_seconds": 1}}}
                    },
                }
            ),
            encoding="utf-8",
        )
        await service.reload()
        pending = state.get_pending_config_candidate(config_domain="gateway")
        assert pending is not None
        candidate_ref = service.status().candidate_ref
        assert candidate_ref is not None
        restart_intent = state.get_gateway_restart_intent(
            candidate_ref=candidate_ref
        )
        assert restart_intent is not None
        service.record_pending_restart_failure(
            candidate_ref=candidate_ref,
            target_generation=restart_intent.target_generation,
            fencing_token=restart_intent.fencing_token,
            error="新 generation 启动失败",
        )
        failed_intent = state.get_gateway_restart_intent(candidate_ref=candidate_ref)
        assert failed_intent is not None
        assert failed_intent.state == "recovery_required"

        proof = service.build_pending_restart_health_proof(
            candidate_ref=candidate_ref,
            generation=failed_intent.target_generation,
            consumer_health_digests=_gateway_consumer_health_digests(),
        )
        wrong_proof = {**proof, "effective_digest": "wrong"}
        with pytest.raises(ConfigConflictError, match="health proof"):
            service.resolve_pending_restart(
                candidate_ref=candidate_ref,
                health_proof=wrong_proof,
            )
        assert (
            state.get_gateway_restart_intent(candidate_ref=candidate_ref).state
            == "recovery_required"
        )
        assert (
            state.get_pending_config_candidate(config_domain="gateway").state
            == "recovery_required"
        )

        service.begin_pending_restart(candidate_ref=candidate_ref)
        retry_claim = state.get_config_apply_claim(config_domain="gateway")
        assert retry_claim is not None
        state.record_gateway_runtime_generation(
            generation_id=failed_intent.target_generation,
            process_id=102,
            loaded_source="pending",
            candidate_id=pending.candidate_id,
            active_revision=(
                state.get_active_config_snapshot("gateway").active_revision
            ),
            pending_revision=pending.pending_revision,
            candidate_digest=pending.candidate_digest,
            effective_digest=pending.effective_digest,
            secret_binding_digest=proof["secret_binding_digest"],
            fencing_token=retry_claim.fencing_token,
            listener_state="reserved",
            state="healthy",
            health_proof=proof,
        )
        service.resolve_pending_restart(
            candidate_ref=candidate_ref,
            health_proof=proof,
        )
        active = state.get_active_config_snapshot("gateway")
        resolved = state.get_pending_config_candidate(config_domain="gateway")
        assert active is not None
        assert resolved is not None
        assert active.effective_digest == resolved.effective_digest
        assert resolved.state == "active"
        assert [event.result for event in service.list_events()] == [
            "restart_required",
            "recovery_required",
            "applied",
        ]
    finally:
        state.close()


@pytest.mark.asyncio
async def test_gateway_pending_discard_requires_safe_active_baseline(
    tmp_path: Path,
) -> None:
    config_path, schema_path = _write_gateway_config(tmp_path, [])
    state = GatewayStateStore(path=tmp_path / "gateway.sqlite")
    try:
        initial = load_gateway_config(
            config_path=config_path,
            schema_path=schema_path,
            state_store=state,
        )
        service = GatewayConfigReloadService(
            state_store=state,
            config=initial,
            config_path=config_path,
            local_config_path=tmp_path / "gateway_local.jsonc",
            schema_path=schema_path,
        )
        service.initialize_active_snapshot()
        config_path.write_text(
            json.dumps(
                {
                    "config_version": 1,
                    "workspaces": [],
                    "runtime": {
                        "gateway": {"process": {"health": {"poll_interval_seconds": 1}}}
                    },
                }
            ),
            encoding="utf-8",
        )
        await service.reload()
        pending = state.get_pending_config_candidate(config_domain="gateway")
        active = state.get_active_config_snapshot("gateway")
        assert pending is not None
        assert active is not None
        candidate_ref = service.status().candidate_ref
        assert candidate_ref is not None

        with pytest.raises(ConfigConflictError, match="安全 active"):
            service.discard_pending_restart(
                candidate_ref=candidate_ref,
                expected_active_revision=active.active_revision,
                expected_active_digest="stale",
            )
        assert (
            state.get_pending_config_candidate(config_domain="gateway").state
            == "pending_restart"
        )

        status = service.discard_pending_restart(
            candidate_ref=candidate_ref,
            expected_active_revision=active.active_revision,
            expected_active_digest=active.effective_digest,
        )
        assert status.state == "discarded"
        assert status.reason is None
        assert (
            state.get_pending_config_candidate(config_domain="gateway").state
            == "discarded"
        )
        assert service.list_events()[-1].result == "discarded"
    finally:
        state.close()
