"""正式 Gateway 入口读取明确父链，来源定义随原快照恢复。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.gateway.config import GatewayConfigReloadService, load_gateway_config
from app.gateway.control.gateway_state import GatewayStateStore
from configs.installer import install_user_configuration


@pytest.fixture
def gateway_files(tmp_path: Path):
    root = tmp_path / "config"
    install_user_configuration(config_root=root, profile="default")
    return root


def test_installed_entry_has_only_explicit_ancestors(gateway_files: Path):
    root = gateway_files
    config = load_gateway_config(
        config_path=root / "gateway.jsonc", entry_path=root / "gateway_local.jsonc",
        schema_path=root / "gateway_schema.jsonc",
    )
    assert config.payload["config_version"] == 6
    assert [s.layer for s in config.source_details] == ["inline", "user", "user_local"]
    assert config.payload["network"] == {"proxy": {"mode": "environment"}}
    assert json.loads((root / "gateway.jsonc").read_text())["settings"] == {}


def test_custom_parent_bypasses_user_source(gateway_files: Path):
    root = gateway_files
    (root / "gateway.jsonc").write_text('invalid ignored file')
    custom = root / "custom.jsonc"
    custom.write_text(json.dumps({
        "config_version": 6, "extends": "builtin:gateway", "settings": {
            "gateway": {"logging": {"runtime": {"level": "debug"}}},
        },
    }))
    (root / "gateway_local.jsonc").write_text(json.dumps({
        "config_version": 6, "extends": "custom.jsonc", "settings": {},
    }))
    config = load_gateway_config(config_path=root / "gateway.jsonc", entry_path=root / "gateway_local.jsonc",
                                 schema_path=root / "gateway_schema.jsonc")
    assert config.payload["logging"]["runtime"]["level"] == "debug"
    assert root / "gateway.jsonc" not in config.source_paths
    assert [s.layer for s in config.source_details] == ["inline", "source", "user_local"]


def test_missing_parent_never_uses_previous_fixed_layers(gateway_files: Path):
    root = gateway_files
    (root / "gateway_local.jsonc").write_text(json.dumps({
        "config_version": 6, "extends": "missing.jsonc", "settings": {},
    }))
    with pytest.raises(FileNotFoundError):
        load_gateway_config(config_path=root / "gateway.jsonc", entry_path=root / "gateway_local.jsonc",
                            schema_path=root / "gateway_schema.jsonc")


def test_active_restores_its_source_documents_without_current_parent(gateway_files: Path):
    root = gateway_files
    args = {"config_path": root / "gateway.jsonc", "entry_path": root / "gateway_local.jsonc",
            "schema_path": root / "gateway_schema.jsonc", "gateway_id": "gateway-test"}
    with GatewayStateStore(path=root / "gateway.sqlite") as store:
        config = load_gateway_config(**args, state_store=store)
        service = GatewayConfigReloadService(
            state_store=store, config=config, config_path=root / "gateway.jsonc",
            local_config_path=root / "gateway_local.jsonc", gateway_id="gateway-test",
        )
        service.initialize_active_snapshot()
        (root / "gateway.jsonc").write_text('invalid current source')
        restored = load_gateway_config(**args, state_store=store, startup=True)
        assert restored.payload == config.payload
        assert restored.graph.source_documents == config.graph.source_documents


@pytest.fixture
def source_service(gateway_files: Path):
    root = gateway_files
    args = {"config_path": root / "gateway.jsonc", "entry_path": root / "gateway_local.jsonc",
            "schema_path": root / "gateway_schema.jsonc", "gateway_id": "gateway-test"}
    with GatewayStateStore(path=root / "gateway.sqlite") as store:
        config = load_gateway_config(**args, state_store=store)
        service = GatewayConfigReloadService(
            state_store=store, config=config, config_path=root / "gateway.jsonc",
            local_config_path=root / "gateway_local.jsonc", schema_path=root / "gateway_schema.jsonc",
            gateway_id="gateway-test",
        )
        service.initialize_active_snapshot()
        yield service, store, root, args


def write_node(path: Path, *, parent: str, settings: dict[str, object] | None = None):
    path.write_text(json.dumps({"config_version": 6, "extends": parent, "settings": settings or {}}))


@pytest.mark.asyncio
async def test_parent_switch_updates_sources_without_business_revision(source_service):
    service, store, root, args = source_service
    initial = store.get_active_config_snapshot("gateway")
    write_node(root / "alternative.jsonc", parent="builtin:gateway")
    write_node(root / "gateway_local.jsonc", parent="alternative.jsonc")
    await service.reload()
    current = store.get_active_config_snapshot("gateway")
    assert current.active_revision == initial.active_revision
    assert current.effective_digest == initial.effective_digest
    assert current.source_baseline != initial.source_baseline
    assert "gateway_mutable_override" not in current.source_baseline
    # 旧物化行仍存在，但不能被当作当前依赖或重新参与合并。
    assert store.get_source_layer("gateway_mutable_override") is not None
    restored = load_gateway_config(**args, state_store=store, startup=True)
    assert restored.graph.source_documents == service.config.graph.source_documents
    assert root / "gateway.jsonc" not in restored.source_paths
    assert not service.list_events()


@pytest.mark.asyncio
async def test_parent_change_during_runtime_apply_rejects_promotion(source_service):
    from app.gateway.runtime.consumer_protocol import GatewayRuntimeConsumerApplyResult
    from app.services.infrastructure.config.state import ConfigConflictError

    service, store, root, _ = source_service
    initial = store.get_active_config_snapshot("gateway")
    write_node(root / "gateway.jsonc", parent="builtin:gateway", settings={
        "gateway": {"logging": {"runtime": {"level": "debug"}}},
    })

    async def apply(candidate, previous, fence, assert_claim):
        assert_claim()
        write_node(root / "gateway.jsonc", parent="builtin:gateway", settings={
            "gateway": {"logging": {"runtime": {"level": "error"}}},
        })
        return GatewayRuntimeConsumerApplyResult(stages=(), proofs=())

    service._on_runtime_config = apply
    with pytest.raises(ConfigConflictError):
        await service.reload()
    assert store.get_active_config_snapshot("gateway") == initial
    assert service.config.payload["logging"]["runtime"]["level"] != "debug"
    assert service.status().state == "conflict"


@pytest.mark.asyncio
async def test_watcher_repairs_missing_parent_directory(source_service):
    service, store, root, _ = source_service
    await service.start()
    try:
        write_node(root / "gateway_local.jsonc", parent="new_parent/parent.jsonc")
        async with asyncio.timeout(8):
            while service.status().healthy:
                await asyncio.sleep(0.05)
        assert root / "new_parent/parent.jsonc" in service._watcher._candidate_paths
        (root / "new_parent").mkdir()
        write_node(root / "new_parent/parent.jsonc", parent="builtin:gateway", settings={
            "gateway": {"logging": {"runtime": {"level": "debug"}}},
        })
        async with asyncio.timeout(8):
            while service.config.payload["logging"]["runtime"]["level"] != "debug":
                await asyncio.sleep(0.05)
        assert service.status().healthy
        assert store.get_active_config_snapshot("gateway").payload["logging"]["runtime"]["level"] == "debug"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_hot_parent_switch_ignores_retired_source_rows(source_service):
    service, store, root, _ = source_service
    initial = store.get_active_config_snapshot("gateway")
    write_node(root / "alternative.jsonc", parent="builtin:gateway", settings={
        "gateway": {"logging": {"runtime": {"level": "debug"}}},
    })
    write_node(root / "gateway_local.jsonc", parent="alternative.jsonc")
    await service.reload()
    current = store.get_active_config_snapshot("gateway")
    assert current.active_revision > initial.active_revision
    assert current.payload["logging"]["runtime"]["level"] == "debug"
    assert "gateway_mutable_override" not in current.source_baseline
    assert store.get_source_layer("gateway_mutable_override") is not None


@pytest.mark.asyncio
async def test_source_writer_rechecks_all_parents_before_file_write(source_service):
    from app.schemas.gateway.config import GatewayConfigSourceEditRequest
    from app.services.infrastructure.config.state import ConfigConflictError

    service, _, root, _ = source_service
    key = "gateway_local_mutable_override"
    document = service.get_source_document(source_key=key)
    original = (root / "gateway_local.jsonc").read_bytes()
    text = json.dumps({"config_version": 6, "extends": "user:gateway", "settings": {
        "gateway": {"logging": {"runtime": {"level": "debug"}}},
    }})
    request = GatewayConfigSourceEditRequest(
        gateway_id=document.gateway_id, source_key=key, jsonc=text, idempotency_key="parent-cas",
        base_file_digest=document.file_digest, base_layer_revision=document.layer_revision,
        base_layer_digest=document.layer_digest, expected_active_revision=document.active_revision,
        expected_active_digest=document.active_digest,
    )

    def change_parent():
        write_node(root / "gateway.jsonc", parent="builtin:gateway", settings={
            "gateway": {"logging": {"runtime": {"level": "error"}}},
        })

    with pytest.raises(ConfigConflictError, match="父依赖"):
        await service.save_source_edit(source_key=key, request=request, before_write=change_parent)
    assert (root / "gateway_local.jsonc").read_bytes() == original


def test_invalid_proxy_is_rejected_before_source_materialization(gateway_files: Path):
    root = gateway_files
    write_node(root / "gateway.jsonc", parent="builtin:gateway", settings={
        "network": {"proxy": {"mode": "explicit", "url": "http://user:password@example.com"}},
    })
    with GatewayStateStore(path=root / "gateway.sqlite") as store:
        with pytest.raises(ValueError):
            load_gateway_config(config_path=root / "gateway.jsonc", schema_path=root / "gateway_schema.jsonc", state_store=store)
        assert store.get_source_layer("gateway_mutable_override") is None


def test_same_file_profile_is_materialized_once(gateway_files: Path):
    root = gateway_files
    (root / "gateway_local.jsonc").write_text(json.dumps({
        "config_version": 6, "extends": "#/profiles/network", "settings": {},
        "profiles": {"network": {"extends": "user:gateway", "settings": {"network": {"proxy": {"mode": "direct"}}}}},
    }))
    config = load_gateway_config(config_path=root / "gateway.jsonc", entry_path=root / "gateway_local.jsonc",
                                 schema_path=root / "gateway_schema.jsonc")
    assert len(config.source_details) == 3
    assert config.payload["network"] == {"proxy": {"mode": "direct"}}


@pytest.mark.asyncio
async def test_restart_promotion_rejects_changed_parent_before_acquiring_claim(source_service):
    from app.gateway.config import REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS
    from app.services.infrastructure.config.state import ConfigConflictError

    service, store, root, _ = source_service
    initial = store.get_active_config_snapshot("gateway")
    write_node(root / "gateway.jsonc", parent="builtin:gateway", settings={
        "gateway": {"runtime": {"gateway": {"process": {"health": {"poll_interval_seconds": 1}}}}},
    })
    await service.reload()
    status = service.status()
    assert status.state == "pending_restart"
    intent = store.get_gateway_restart_intent(candidate_ref=status.candidate_ref)
    proof = service.build_pending_restart_health_proof(
        candidate_ref=status.candidate_ref, generation=intent.target_generation,
        consumer_health_digests={key: "a" * 64 for key in REQUIRED_GATEWAY_CONSUMER_HEALTH_IDS},
    )
    # 准备完成后父文件被外部编辑，数据库来源行尚未被 watcher 导入。
    write_node(root / "gateway.jsonc", parent="builtin:gateway", settings={
        "gateway": {"runtime": {"gateway": {"process": {"health": {"poll_interval_seconds": 2}}}}},
    })
    with pytest.raises(ConfigConflictError, match="父依赖"):
        service.record_pending_restart_proof(candidate_ref=status.candidate_ref, health_proof=proof)
    assert store.get_active_config_snapshot("gateway") == initial
    assert store.get_config_apply_claim(config_domain="gateway") is None
    assert store.get_gateway_restart_intent(candidate_ref=status.candidate_ref).state == "pending"


@pytest.mark.asyncio
async def test_source_only_commit_rejects_stale_active_provenance(source_service):
    from app.services.infrastructure.config.state import ConfigConflictError

    service, store, root, _ = source_service
    initial = store.get_active_config_snapshot("gateway")
    write_node(root / "alternative.jsonc", parent="builtin:gateway")
    write_node(root / "gateway_local.jsonc", parent="alternative.jsonc")
    await service.reload()
    current = store.get_active_config_snapshot("gateway")
    assert current.active_revision == initial.active_revision
    with pytest.raises(ConfigConflictError, match="active CAS"):
        store.refresh_active_config_sources(
            config_domain="gateway", expected_active_revision=initial.active_revision,
            effective_digest=initial.effective_digest, expected_source_baseline=initial.source_baseline,
            source_baseline=current.source_baseline, source_generation=current.source_generation,
            layer_revisions=current.layer_revisions, layer_digests=current.layer_digests,
        )
    assert store.get_active_config_snapshot("gateway") == current


def test_source_inspection_inherits_actual_profile_parent(source_service):
    service, _, root, _ = source_service
    (root / "gateway_local.jsonc").write_text(json.dumps({
        "config_version": 6, "extends": "#/profiles/network", "settings": {},
        "profiles": {"network": {"extends": "user:gateway", "settings": {"network": {"proxy": {"mode": "direct"}}}}},
    }))
    document = service.get_source_document(source_key="gateway_local_mutable_override")
    assert document.source_inherited["network"] == {"proxy": {"mode": "direct"}}
    assert document.source_effective["network"] == {"proxy": {"mode": "direct"}}
    assert document.model_dump(mode="json")["source_inherited"] == document.source_inherited


def test_absent_source_preview_keeps_real_disk_dependency(gateway_files: Path):
    root = gateway_files
    path = root / "gateway.jsonc"
    path.unlink()
    config = load_gateway_config(config_path=path, schema_path=root / "gateway_schema.jsonc",
        persist_migrations=False, source_overrides={"gateway_mutable_override": {
            "config_version": 6, "extends": "builtin:gateway", "settings": {},
        }})
    assert config.payload["config_version"] == 6
    assert not path.exists()
    assert config.graph.dependencies[path].presence == "absent"
    GatewayConfigReloadService._verify_source_dependencies(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("reload_parent", [False, True])
async def test_source_edit_rejects_retired_source_without_writing(source_service, reload_parent):
    from app.schemas.gateway.config import GatewayConfigSourceEditRequest
    from app.services.infrastructure.config.state import ConfigConflictError

    service, store, root, _ = source_service
    key = "gateway_mutable_override"
    document = service.get_source_document(source_key=key)
    original = (root / "gateway.jsonc").read_bytes()
    initial = store.get_active_config_snapshot("gateway")
    request = GatewayConfigSourceEditRequest(
        gateway_id=document.gateway_id, source_key=key,
        jsonc=json.dumps({"config_version": 6, "extends": "builtin:gateway", "settings": {
            "gateway": {"logging": {"runtime": {"level": "debug"}}},
        }}), idempotency_key="retired-source",
        base_file_digest=document.file_digest, base_layer_revision=document.layer_revision,
        base_layer_digest=document.layer_digest, expected_active_revision=document.active_revision,
        expected_active_digest=document.active_digest,
    )
    write_node(root / "alternative.jsonc", parent="builtin:gateway")
    write_node(root / "gateway_local.jsonc", parent="alternative.jsonc")
    if reload_parent:
        await service.reload()
    with pytest.raises(ConfigConflictError, match="父链"):
        await service.save_source_edit(source_key=key, request=request)
    assert (root / "gateway.jsonc").read_bytes() == original
    assert store.get_active_config_snapshot("gateway").active_revision == initial.active_revision
    assert store.get_pending_config_candidate(config_domain="gateway", idempotency_key="retired-source") is None
