"""Gateway 支持范围内正常升级；无验收基线的 Workspace 旧来源保持原件。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.core.sqlite_state import SQLiteProcessOwnership
from app.gateway.control.gateway_state import (
    _GATEWAY_MIGRATIONS,
    GatewayStateStore,
)
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.cli import main
from configs.maintenance import runtime_logging_commit as publisher
from configs.runtime import read_jsonc_object
from tests.unit.configs.conftest import LegacyGateway


@pytest.fixture
def startup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd())])
    return home


@pytest.fixture
def legacy_workspace_payload() -> dict[str, object]:
    payload = read_jsonc_object(Path.cwd() / "configs/workspace_inline.jsonc")
    payload["config_version"] = 4
    return payload


@pytest.mark.parametrize("upgrade_gateway", [False, True])
def test_initialize_current_workspace_reenters_without_rewriting_it(
    startup_home: Path, capsys: pytest.CaptureFixture[str], upgrade_gateway: bool,
) -> None:
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"] == {}
    config = startup_home / "config"
    workspace = config / "workspace.jsonc"
    assert read_jsonc_object(workspace)["config_version"] == 4
    original = workspace.read_bytes()
    if upgrade_gateway:
        gateway = config / "gateway.jsonc"
        gateway.write_text(json.dumps({"config_version": 4}), encoding="utf-8")

    main()
    upgrades = json.loads(capsys.readouterr().out)["upgrades"]
    if upgrade_gateway:
        assert upgrades["configuration"]["state"] == "completed"
    else:
        assert upgrades == {}
    assert workspace.read_bytes() == original
    after = {path: path.read_bytes() for path in config.rglob("*") if path.is_file()}
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"] == {}
    assert {path: path.read_bytes() for path in config.rglob("*") if path.is_file()} == after


@pytest.mark.parametrize("domain,current_version", [("gateway", 6), ("workspace", 4)])
@pytest.mark.parametrize("old_version", [False, True])
def test_initialize_absent_source_upgrades_only_its_old_version(
    startup_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    domain: str, current_version: int, old_version: bool,
) -> None:
    workspace = startup_home / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setattr("sys.argv", [
        "boxteam", "initialize", "--project-root", str(Path.cwd()), "--workspace", str(workspace),
    ])
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"] == {}
    database = startup_home / "state/gateway/gateway.sqlite"
    key = f"{domain}_mutable_override"
    with (GatewayStateStore(path=database) if domain == "gateway"
          else WorkspaceStateStore(workspace_root=workspace)) as store:
        source = store.sync_config_source(
            config_key=key, uri=None, config_version=1 if old_version else current_version,
            presence="absent", payload=None, layer_digest=None,
        )

    for attempt in range(2):
        if domain == "workspace" and old_version:
            with pytest.raises(ValueError, match="缺少已验收升级基线"):
                main()
            with WorkspaceStateStore(workspace_root=workspace) as store:
                assert store.get_source_layer(key) == source
            continue
        main()
        upgrades = json.loads(capsys.readouterr().out)["upgrades"]
        if old_version and attempt == 0:
            assert upgrades["configuration"]["state"] == "completed"
        else:
            assert upgrades == {}
        with (GatewayStateStore(path=database) if domain == "gateway"
              else WorkspaceStateStore(workspace_root=workspace)) as store:
            current = store.get_source_layer(key)
        assert current is not None
        assert current.config_version == current_version
        assert current.presence == "absent" and current.payload is None and current.layer_digest is None
        if old_version and attempt == 0:
            assert current.layer_revision == source.layer_revision + 1
        else:
            assert current == source
        source = current


@pytest.mark.parametrize("preview", [False, True])
def test_initialize_rejects_legacy_models_without_an_accepted_baseline(
    startup_home: Path, monkeypatch: pytest.MonkeyPatch, preview: bool,
    legacy_model_configuration: dict[str, object],
) -> None:
    path = startup_home / "config/workspace.jsonc"
    path.parent.mkdir(parents=True)
    original = ('// 保留原始注释\n' + json.dumps(legacy_model_configuration)).encode()
    path.write_bytes(original)
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", *(["--preview"] if preview else [])])
    with pytest.raises(ValueError, match="缺少已验收升级基线"):
        main()
    assert list(path.parent.iterdir()) == [path]
    assert path.read_bytes() == original




@pytest.mark.parametrize("version", [1, 3, 999])
@pytest.mark.parametrize("preview", [False, True])
def test_initialize_preserves_unsupported_workspace_source_before_installation(
    startup_home: Path, monkeypatch: pytest.MonkeyPatch, version: int, preview: bool,
) -> None:
    path = startup_home / "config/workspace.jsonc"
    path.parent.mkdir(parents=True)
    original = ('// 用户原始注释\n' + json.dumps({"config_version": version, "logger": {"level": "debug"}})).encode()
    path.write_bytes(original)
    before = {file: file.read_bytes() for file in startup_home.rglob("*") if file.is_file()}
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", *(["--preview"] if preview else [])])
    with pytest.raises(ValueError, match="缺少已验收升级基线"):
        main()
    assert {file: file.read_bytes() for file in startup_home.rglob("*") if file.is_file()} == before




def test_initialize_automatically_migrates_supported_identity_and_preserves_backup(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    case = legacy_gateway
    original = case.dump()
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd())])
    main()
    upgrades = json.loads(capsys.readouterr().out)["upgrades"]
    result = upgrades["workspace_identity"]
    assert result["state"] == "completed"
    with closing(sqlite3.connect(Path(result["backup_root"]) / "gateway.before.sqlite")) as connection:
        assert "\n".join(connection.iterdump()) == original
    with closing(sqlite3.connect(case.database)) as connection:
        rows = connection.execute("SELECT gateway_id,workspace_id FROM gateway_workspace_registry").fetchall()
        current = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert upgrades["sqlite"] == {"state": "pending_owner_startup", "databases": [{
        "path": str(case.database), "current_schema_version": current,
        "target_schema_version": len(_GATEWAY_MIGRATIONS),
    }]}
    assert set(rows) == {(case.gateway_id, value["workspace_id"])
                         for alias, value in case.metadata["workspaces"].items() if alias.startswith("gw_local_")}
    after = case.dump()
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"] == {"sqlite": upgrades["sqlite"]}
    assert case.dump() == after


@pytest.mark.parametrize("state", ["restoring", "restored"])
def test_initialize_preserves_explicit_restore_direction(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    from configs.maintenance.workspace_identity_commit import (
        commit_workspace_identity_migration,
    )

    case = legacy_gateway
    original = case.dump()
    commit_workspace_identity_migration(gateway_root=case.root, project_root=Path.cwd())
    journal_path = case.root / ".workspace-identity-migration.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["state"] = "restoring"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    if state == "restored":
        commit_workspace_identity_migration(gateway_root=case.root, project_root=Path.cwd(), restore=True)
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd())])
    with pytest.raises(RuntimeError, match="回退"):
        main()
    assert case.dump() == original
    assert json.loads(journal_path.read_text(encoding="utf-8"))["state"] == "restored"


def test_running_owner_blocks_gateway_upgrade_before_any_file_changes(
    startup_home: Path,
) -> None:
    config = startup_home / "config"
    config.mkdir(parents=True)
    legacy = config / "gateway.jsonc"
    original = json.dumps({"config_version": 4, "logging": {"runtime": {"level": "debug"}}}).encode()
    legacy.write_bytes(original)
    ownership = SQLiteProcessOwnership(startup_home / "state/gateway/gateway.sqlite")
    ownership.acquire()
    try:
        with pytest.raises(RuntimeError, match="已被另一个进程占用"):
            main()
    finally:
        ownership.release()
    assert {path.name for path in config.iterdir()} == {legacy.name, ".runtime-logging-migration.lock"}
    assert not (config / ".runtime-logging-migration.json").exists()
    assert legacy.read_bytes() == original


def test_live_gateway_upgrade_preflight_is_read_only_then_initialization_applies(
    startup_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = startup_home / "config"
    config.mkdir(parents=True)
    path = config / "gateway.jsonc"
    original = b'{"config_version":4,"logging":{"runtime":{"level":"debug"}}}\n'
    path.write_bytes(original)
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--preview"])
    main()
    assert json.loads(capsys.readouterr().out)["upgrade_required"] is True
    assert list(config.iterdir()) == [path] and path.read_bytes() == original
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize"])
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"]["configuration"]["state"] == "completed"
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--preview"])
    main()
    assert json.loads(capsys.readouterr().out)["upgrade_required"] is False


@pytest.mark.parametrize("preview", [True, False])
@pytest.mark.parametrize("fault,reason", [
    ("config_version", "支持范围"),
    ("sqlite_version", "账本版本未知"),
    ("logging_conflict", "需要用户决定"),
    ("target_schema", "配置验证失败"),
    ("identity_conflict", "workspace_identity_mismatch"),
    ("resource_ledger", "旧资源账本非空"),
])
def test_registered_legacy_layout_checks_all_sources_before_any_write(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch,
    legacy_workspace_payload: dict[str, object], preview: bool, fault: str, reason: str,
) -> None:
    case = legacy_gateway
    workspace = case.directory / "workspace-0"
    legacy = workspace / ".boxteam/workspace.jsonc"
    legacy.write_text(json.dumps(legacy_workspace_payload), encoding="utf-8")
    config = case.directory / "home/config"
    config.mkdir(parents=True)
    if fault == "sqlite_version":
        with closing(sqlite3.connect(case.database)) as connection:
            connection.execute("INSERT INTO schema_migrations VALUES (?, 'future')", (len(_GATEWAY_MIGRATIONS) + 1,))
            connection.commit()
    elif fault == "resource_ledger":
        (workspace / ".boxteam/resources.json").write_text('{"records":[{}],"leases":[]}', encoding="utf-8")
    elif fault == "identity_conflict":
        identity_path = workspace / ".boxteam/workspace-identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["workspace_id"] = case.metadata["workspaces"]["gw_local_1"]["workspace_id"]
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
    else:
        payloads = {
            "config_version": {"config_version": 999},
            "logging_conflict": {"config_version": 4, "ui": {"theme": {"default_theme_id": "custom", "custom_themes": []}}},
            "target_schema": {"config_version": 4, "unsupported_setting": True},
        }
        (config / "gateway.jsonc").write_text(json.dumps(payloads[fault]), encoding="utf-8")
    before = {path.relative_to(case.directory): path.read_bytes() if path.is_file() else None
              for path in case.directory.rglob("*")}
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd()),
                                    *(["--preview"] if preview else [])])
    with pytest.raises(ValueError, match=reason):
        main()
    assert {path.relative_to(case.directory): path.read_bytes() if path.is_file() else None
            for path in case.directory.rglob("*")} == before


@pytest.mark.parametrize("preview", [True, False])
@pytest.mark.parametrize("fault", ["generation", "route"])
def test_unsettled_identity_is_rejected_before_gateway_configuration_publish(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch,
    preview: bool, fault: str,
) -> None:
    case = legacy_gateway
    config = case.directory / "home/config"
    config.mkdir(parents=True)
    payload = {"config_version": 4, "logging": {"runtime": {"level": "info"}}}
    (config / "gateway.jsonc").write_text(json.dumps(payload), encoding="utf-8")
    with closing(sqlite3.connect(case.database)) as connection:
        if fault == "generation":
            connection.execute(
                "INSERT INTO gateway_runtime_generation "
                "(config_domain,generation_id,loaded_source,effective_digest,listener_state,state,created_at,updated_at) "
                "VALUES ('gateway','unsettled','active','digest','serving','active','before','before')"
            )
        else:
            raw = connection.execute("SELECT payload_json FROM gateway_workspace_registry WHERE workspace_id='gw_local_0'").fetchone()[0]
            target = json.loads(raw)
            target["active_request_count"] = 1
            connection.execute("UPDATE gateway_workspace_registry SET payload_json=? WHERE workspace_id='gw_local_0'", (json.dumps(target),))
        connection.commit()
    before = {path.relative_to(case.directory): path.read_bytes() if path.is_file() else None
              for path in case.directory.rglob("*")}
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd()),
                                    *(["--preview"] if preview else [])])
    with pytest.raises(ValueError, match="身份迁移存在冲突或未结算来源"):
        main()
    assert {path.relative_to(case.directory): path.read_bytes() if path.is_file() else None
            for path in case.directory.rglob("*")} == before


@pytest.mark.parametrize("legacy_gateway", [True], indirect=True)
@pytest.mark.parametrize("preview", [True, False])
def test_unverified_remote_is_rejected_before_gateway_configuration_publish(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch,
    preview: bool,
) -> None:
    case = legacy_gateway
    config = case.directory / "home/config"
    config.mkdir(parents=True)
    payload = {"config_version": 4, "logging": {"runtime": {"level": "info"}}}
    (config / "gateway.jsonc").write_text(json.dumps(payload), encoding="utf-8")
    before = {path.relative_to(case.directory): path.read_bytes() if path.is_file() else None
              for path in case.directory.rglob("*")}
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd()),
                                    *(["--preview"] if preview else [])])
    with pytest.raises(ValueError, match="remote_identity_probe_failed"):
        main()
    assert {path.relative_to(case.directory): path.read_bytes() if path.is_file() else None
            for path in case.directory.rglob("*")} == before


def test_only_live_preview_defers_generation_settlement_to_original_stop(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    case = legacy_gateway
    with closing(sqlite3.connect(case.database)) as connection:
        connection.execute(
            "INSERT INTO gateway_runtime_generation "
            "(config_domain,generation_id,process_id,loaded_source,effective_digest,listener_state,state,created_at,updated_at) "
            "VALUES ('gateway','live',?,'active','digest','serving','active','before','before')", (os.getpid(),),
        )
        connection.commit()
    before = case.dump()
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--preview", "--project-root", str(Path.cwd())])
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"]["workspace_identity"] == {"state": "migration_required"}
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd())])
    with pytest.raises(ValueError, match="unsettled_control_state"):
        main()
    assert case.dump() == before


@pytest.mark.parametrize("preview", [False, True])
def test_legacy_workspace_layout_is_preserved_without_an_accepted_baseline(
    startup_home: Path, monkeypatch: pytest.MonkeyPatch,
    legacy_workspace_payload: dict[str, object], preview: bool,
) -> None:
    config = startup_home / "config"
    config.mkdir(parents=True)
    legacy = config / "boxteam.jsonc"
    legacy.write_text(json.dumps(legacy_workspace_payload), encoding="utf-8")
    original = legacy.read_bytes()
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", *(["--preview"] if preview else [])])
    with pytest.raises(ValueError, match="缺少已验收升级基线"):
        main()
    assert list(startup_home.rglob("*")) == [config, legacy]
    assert legacy.read_bytes() == original




def test_registered_and_explicit_workspace_layout_refuses_before_identity_publication(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch,
    legacy_workspace_payload: dict[str, object],
) -> None:
    case = legacy_gateway
    workspace = case.directory / "workspace-0"
    source = workspace / ".boxteam/boxteam.jsonc"
    source.write_text(json.dumps(legacy_workspace_payload), encoding="utf-8")
    before = {path: path.read_bytes() for path in case.directory.rglob("*") if path.is_file()}
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd()),
                                    "--workspace", str(workspace)])
    with pytest.raises(ValueError, match="缺少已验收升级基线"):
        main()
    assert {path: path.read_bytes() for path in case.directory.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("state", ["prepared", "applying"])
def test_initialize_resumes_original_identity_plan_after_publish_interruption(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], state: str,
) -> None:
    case = legacy_gateway
    journal_path = case.root / ".workspace-identity-migration.json"
    original_write = publisher.atomic_write

    def interrupt(path: Path, content: bytes, mode: int = 0o600) -> None:
        if (state == "prepared" and path == journal_path and json.loads(content).get("state") == "applying"
                or state == "applying" and path == case.root / "navigation/workspace-tree.json"):
            raise OSError("身份发布中断")
        original_write(path, content, mode)

    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd())])
    with monkeypatch.context() as patch:
        patch.setattr(publisher, "atomic_write", interrupt)
        with pytest.raises(OSError, match="身份发布中断"):
            main()
    frozen = json.loads(journal_path.read_text(encoding="utf-8"))
    assert frozen["state"] == state
    backups = {path: path.read_bytes() for path in Path(frozen["backup_root"]).rglob("*") if path.is_file()}
    with monkeypatch.context() as patch:
        patch.setattr("sys.argv", ["boxteam", "initialize", "--preview"])
        main()
    assert json.loads(capsys.readouterr().out)["upgrades"] == {"recovery": {"state": "migration_required"}}
    assert json.loads(journal_path.read_text(encoding="utf-8")) == frozen
    main()
    result = json.loads(capsys.readouterr().out)["upgrades"]["workspace_identity"]
    assert result["state"] == "completed"
    assert result["migration_id"] == frozen["migration_id"]
    assert result["backup_root"] == frozen["backup_root"]
    assert list(Path(frozen["backup_root"]).parent.iterdir()) == [Path(frozen["backup_root"])]
    assert {path: path.read_bytes() for path in backups} == backups
    with closing(sqlite3.connect(case.database)) as connection:
        rows = connection.execute("SELECT gateway_id,workspace_id FROM gateway_workspace_registry").fetchall()
    assert set(rows) == {(case.gateway_id, value["workspace_id"])
                         for alias, value in case.metadata["workspaces"].items() if alias.startswith("gw_local_")}


@pytest.mark.parametrize("preview", [True, False])
def test_old_home_source_is_not_moved_before_other_config_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_workspace_payload: dict[str, object], preview: bool,
) -> None:
    home = tmp_path / "user-home"
    source = home / ".boxteam/boxteam.jsonc"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(legacy_workspace_payload), encoding="utf-8")
    config = home / ".boxteams/config"
    config.mkdir(parents=True)
    (config / "workspace_local.jsonc").write_text('{"config_version":999}', encoding="utf-8")
    monkeypatch.delenv("BOXTEAM_HOME", raising=False)
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    monkeypatch.delenv("BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--home", str(home),
                                    *(["--preview"] if preview else [])])
    before = {path.relative_to(home): path.read_bytes() if path.is_file() else None for path in home.rglob("*")}
    with pytest.raises(ValueError, match="缺少已验收升级基线"):
        main()
    assert {path.relative_to(home): path.read_bytes() if path.is_file() else None for path in home.rglob("*")} == before


@pytest.mark.parametrize("preview", [True, False])
@pytest.mark.parametrize("inherited", [True, False])
def test_startup_loads_gateway_root_from_env_without_overriding_inherited_value(
    startup_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    preview: bool, inherited: bool,
) -> None:
    root = startup_home / "configured-gateway"
    root.mkdir(parents=True)
    database = root / "gateway.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)")
        connection.execute("INSERT INTO schema_migrations VALUES(999, 'future')")
        connection.commit()
    config = startup_home / "config"
    config.mkdir()
    (config / ".env").write_text(f"BOXTEAM_GATEWAY_ROOT={root}\n", encoding="utf-8")
    if inherited:
        monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(startup_home / "inherited-gateway"))
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", *(["--preview"] if preview else [])])
    before = database.read_bytes()
    if inherited:
        main()
        assert json.loads(capsys.readouterr().out)["upgrades"] == {}
    else:
        with pytest.raises(ValueError, match="账本版本未知"):
            main()
        assert sorted(path.name for path in config.iterdir()) == [".env"]
    assert database.read_bytes() == before


@pytest.mark.parametrize("preview", [True, False])
def test_selected_gateway_checks_extra_old_gateway_before_moving_it(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch,
    legacy_workspace_payload: dict[str, object], preview: bool,
) -> None:
    case = legacy_gateway
    home = case.directory / "user-home"
    boxteam_home = home / ".boxteams"
    legacy_gateway_root = boxteam_home / "boxteam_workspace/.boxteam/gateway"
    shutil.copytree(case.root, legacy_gateway_root)
    with closing(sqlite3.connect(legacy_gateway_root / "gateway.sqlite")) as connection:
        connection.execute("INSERT INTO schema_migrations VALUES (?, 'future')", (len(_GATEWAY_MIGRATIONS) + 1,))
        connection.commit()
    workspace = case.directory / "workspace-0"
    (workspace / ".boxteam/workspace.jsonc").write_text(json.dumps(legacy_workspace_payload), encoding="utf-8")
    config = boxteam_home / "config"
    config.mkdir()
    (config / ".env").write_text(f"BOXTEAM_GATEWAY_ROOT={case.directory / 'selected-gateway'}\n", encoding="utf-8")
    monkeypatch.delenv("BOXTEAM_HOME", raising=False)
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    monkeypatch.delenv("BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--home", str(home),
                                    *(["--preview"] if preview else [])])
    before = {path.relative_to(case.directory): path.read_bytes() if path.is_file() else None
              for path in case.directory.rglob("*")}
    with pytest.raises(ValueError, match="账本版本未知"):
        main()
    assert {path.relative_to(case.directory): path.read_bytes() if path.is_file() else None
            for path in case.directory.rglob("*")} == before


def test_initialize_previews_and_retires_only_empty_resource_ledger(
    startup_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    ledger = workspace / ".boxteam/resources.json"
    ledger.parent.mkdir(parents=True)
    original = b'{\n  "records": [], "leases": []\n}\n'
    ledger.write_bytes(original)
    arguments = ["boxteam", "initialize", "--workspace", str(workspace)]
    monkeypatch.setattr("sys.argv", [*arguments, "--preview"])
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"] == {"empty_resource_ledgers": {
        "state": "migration_required", "workspaces": [str(workspace)],
    }}
    assert ledger.read_bytes() == original and list(ledger.parent.iterdir()) == [ledger]
    assert not startup_home.exists()
    monkeypatch.setattr("sys.argv", arguments)
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"]["empty_resource_ledgers"] == {
        "state": "completed", "workspaces": [str(workspace)],
    }
    assert not ledger.exists()
    assert ledger.with_name("resources.json.retired-empty.bak").read_bytes() == original
    assert not (workspace / ".boxteam/resources.sqlite").exists()
    assert not (workspace / ".boxteam/state/workspace.sqlite").exists()
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"] == {}
