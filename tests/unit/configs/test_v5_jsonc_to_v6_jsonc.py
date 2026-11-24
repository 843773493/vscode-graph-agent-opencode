"""Gateway 平面来源通过显式维护链进入 v6 配置节点。"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.core.config_sources import config_revision
from app.gateway.config.loader import load_gateway_config
from app.gateway.control.gateway_state import GatewayStateStore
from configs.maintenance.runtime_logging_commit import commit_runtime_logging_migration
from configs.maintenance.runtime_logging_migration import (
    preview_runtime_logging_migration,
)
from configs.maintenance.startup import prepare_startup_migrations
from configs.runtime import read_jsonc_object


def _write(path: Path, payload: dict[str, object]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
    path.write_bytes(content)
    return content


def test_flat_gateway_source_reaches_real_v6_loader_and_restores_exact_original(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / "config/gateway.jsonc"
    original = _write(path, {
        "config_version": 5, "workspaces": [],
        "logging": {"runtime": {"level": "warning", "pretty": False}},
    })

    result = commit_runtime_logging_migration(boxteam_home=home, project_root=Path.cwd())

    assert result["state"] == "completed"
    document = read_jsonc_object(path)
    assert document["config_version"] == 6
    assert document["extends"] == "builtin:gateway"
    loaded = load_gateway_config(
        config_path=path, local_config_path=home / "config/gateway_local.jsonc",
        schema_path=Path.cwd() / "configs/gateway_schema.jsonc",
        inline_config_path=Path.cwd() / "configs/gateway_inline.jsonc",
        persist_migrations=False,
    )
    assert loaded.payload["logging"]["runtime"] == {"level": "warning", "pretty": False}
    assert loaded.payload["config_version"] == 6
    assert [source.layer for source in loaded.source_details] == ["inline", "user"]
    assert commit_runtime_logging_migration(boxteam_home=home, project_root=Path.cwd())["state"] == "unchanged"
    assert commit_runtime_logging_migration(boxteam_home=home, project_root=Path.cwd(), restore=True)["state"] == "restored"
    assert path.read_bytes() == original


def test_current_gateway_v6_startup_does_not_write_or_create_backup(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / "config/gateway.jsonc"
    original = _write(path, {
        "config_version": 6, "extends": "builtin:gateway",
        "settings": {"gateway": {"logging": {"runtime": {"level": "debug", "pretty": True}}}},
    })

    assert prepare_startup_migrations(
        boxteam_home=home, project_root=Path.cwd(), gateway_root=home / "state/gateway",
    ) == {}
    assert prepare_startup_migrations(
        boxteam_home=home, project_root=Path.cwd(), gateway_root=home / "state/gateway",
    ) == {}
    assert path.read_bytes() == original
    assert not (home / "config/.runtime-logging-migration.json").exists()
    assert not (home / "config/runtime-logging-migrations").exists()


def test_flat_local_layer_keeps_user_precedence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user = home / "config/gateway.jsonc"
    local = home / "config/gateway_local.jsonc"
    _write(user, {"config_version": 5, "workspaces": [], "logging": {"runtime": {"level": "info", "pretty": True}}})
    _write(local, {"config_version": 5, "logging": {"runtime": {"level": "error", "pretty": False}}})

    assert commit_runtime_logging_migration(boxteam_home=home, project_root=Path.cwd())["state"] == "completed"
    assert read_jsonc_object(user)["extends"] == "builtin:gateway"
    assert read_jsonc_object(local)["extends"] == "user:gateway"
    loaded = load_gateway_config(
        config_path=user, local_config_path=local, entry_path=local,
        schema_path=Path.cwd() / "configs/gateway_schema.jsonc",
        inline_config_path=Path.cwd() / "configs/gateway_inline.jsonc",
        persist_migrations=False,
    )
    assert loaded.payload["logging"]["runtime"] == {"level": "error", "pretty": False}
    assert [source.layer for source in loaded.source_details] == ["inline", "user", "user_local"]


def test_inactive_old_layer_is_validated_before_backup(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user = home / "config/gateway.jsonc"
    local = home / "config/gateway_local.jsonc"
    original = _write(user, {"config_version": 5, "unknown_business_option": True})
    _write(local, {"config_version": 6, "extends": "builtin:gateway", "settings": {}})

    with pytest.raises(ValueError, match="配置验证失败"):
        commit_runtime_logging_migration(boxteam_home=home, project_root=Path.cwd())
    assert user.read_bytes() == original
    assert not (home / "config/.runtime-logging-migration.json").exists()
    assert not (home / "config/runtime-logging-migrations").exists()


def test_candidate_checks_real_parent_chain_before_backup(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user = home / "config/gateway.jsonc"
    local = home / "config/gateway_local.jsonc"
    original = _write(user, {"config_version": 5, "workspaces": []})
    _write(local, {"config_version": 6, "extends": "missing-parent.jsonc", "settings": {}})

    with pytest.raises(FileNotFoundError, match="声明的配置来源不存在"):
        commit_runtime_logging_migration(boxteam_home=home, project_root=Path.cwd())
    assert user.read_bytes() == original
    assert not (home / "config/.runtime-logging-migration.json").exists()
    assert not (home / "config/runtime-logging-migrations").exists()


def test_unknown_version_and_old_active_without_sources_preserve_original(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / "config/gateway.jsonc"
    unknown = _write(path, {"config_version": 7, "workspaces": []})
    with pytest.raises(ValueError, match="版本不在日志迁移支持范围"):
        preview_runtime_logging_migration(boxteam_home=home)
    assert path.read_bytes() == unknown

    legacy = {"config_version": 5, "workspaces": [], "logging": {"runtime": {"level": "info", "pretty": True}}}
    original = _write(path, legacy)
    with GatewayStateStore(path=home / "state/gateway/gateway.sqlite") as store:
        store.ensure_active_config_snapshot(
            config_domain="gateway", payload=legacy, source_baseline={}, source_generation=0,
            layer_revisions={}, layer_digests={}, effective_digest=config_revision(legacy),
            secret_bindings={}, schema_version=5,
        )
    with pytest.raises(ValueError, match="逐层来源文档"):
        commit_runtime_logging_migration(boxteam_home=home, project_root=Path.cwd())
    assert path.read_bytes() == original
    assert not (home / "config/.runtime-logging-migration.json").exists()
    assert not (home / "config/runtime-logging-migrations").exists()


def test_v1_connection_identity_is_kept_in_v6_source(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / "config/gateway.jsonc"
    remote = {"kind": "remote_gateway", "host": "example.test", "username": "developer", "private_key_path": "~/.ssh/key"}
    legacy = {"config_version": 1, "workspaces": [remote]}
    original = _write(path, legacy)
    entries = [{"connection_id": "connection_original", "fingerprint": ["example.test", 22, "developer", "~/.ssh/key", None, 8014]}]
    with GatewayStateStore(path=home / "state/gateway/gateway.sqlite") as store:
        store.set_config(config_key="gateway_connection_ids", config_version=3, payload={"entries": entries})
        store.sync_config_source(
            config_key="gateway_mutable_override", uri=None, config_version=1,
            presence="present", payload=legacy, layer_digest=sha256(original).hexdigest(),
        )

    assert commit_runtime_logging_migration(boxteam_home=home, project_root=Path.cwd())["state"] == "completed"
    document = read_jsonc_object(path)
    assert document["settings"]["gateway"]["workspaces"][0]["connection_id"] == "connection_original"
    with GatewayStateStore(path=home / "state/gateway/gateway.sqlite") as store:
        record = store.get_config("gateway_connection_ids")
        assert record is not None and record.config_version == 4 and record.payload == {"entries": entries}
        loaded = load_gateway_config(
            config_path=path, local_config_path=home / "config/gateway_local.jsonc",
            schema_path=Path.cwd() / "configs/gateway_schema.jsonc",
            inline_config_path=Path.cwd() / "configs/gateway_inline.jsonc",
            state_store=store,
        )
        assert loaded.payload["workspaces"][0]["connection_id"] == "connection_original"
        source = store.get_source_layer("gateway_mutable_override")
        assert source is not None and source.config_version == 6 and source.payload == document


def test_absent_old_source_layer_is_upgraded_without_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with GatewayStateStore(path=home / "state/gateway/gateway.sqlite") as store:
        store.sync_config_source(
            config_key="gateway_mutable_override", uri=None, config_version=5,
            presence="absent", payload=None, layer_digest=None,
        )

    report = prepare_startup_migrations(
        boxteam_home=home, project_root=Path.cwd(), gateway_root=home / "state/gateway",
    )
    assert report["configuration"]["state"] == "completed"
    with GatewayStateStore(path=home / "state/gateway/gateway.sqlite") as store:
        source = store.get_source_layer("gateway_mutable_override")
        assert source is not None and source.config_version == 6 and source.payload is None
