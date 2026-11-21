"""正常初始化自动升级受支持来源，未知版本或配置取舍保留原件。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.core.sqlite_state import SQLiteProcessOwnership
from app.gateway.control.gateway_state import _GATEWAY_MIGRATIONS
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
    assert read_jsonc_object(workspace)["config_version"] == 3
    original = workspace.read_bytes()
    if upgrade_gateway:
        gateway = config / "gateway.jsonc"
        payload = read_jsonc_object(gateway)
        payload["config_version"] = 4
        gateway.write_text(json.dumps(payload), encoding="utf-8")

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


@pytest.mark.parametrize("preview", [False, True])
def test_initialize_migrates_legacy_models_with_backup_and_readonly_preview(
    startup_home: Path, monkeypatch: pytest.MonkeyPatch, preview: bool,
    legacy_model_configuration: dict[str, object], capsys: pytest.CaptureFixture[str],
) -> None:
    path = startup_home / "config/workspace.jsonc"
    path.parent.mkdir(parents=True)
    original = ('// 保留原始注释\n' + json.dumps(legacy_model_configuration)).encode()
    path.write_bytes(original)
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", *(["--preview"] if preview else [])])

    main()
    report = json.loads(capsys.readouterr().out)
    if preview:
        assert report["upgrade_required"] is True
        assert list(path.parent.iterdir()) == [path]
        assert path.read_bytes() == original
        return
    converted = read_jsonc_object(path)
    assert converted["config_version"] == 3
    providers = converted["llm"]["providers"]
    assert list(providers) == ["primary", "backup_1", "backup_2"]
    for old in legacy_model_configuration["llm"]["providers"]:
        assert providers[old["id"]] == {
            "defaults": {**{key: value for key, value in old.items() if key not in ("id", "model", "request_options")},
                         "request_options": {"parameters": {"reasoning_effort": "max", "extra_body": {"custom": True}},
                                             "default_headers": {"X-Test": "kept"}, "no_proxy": True}},
            "models": {"default": {"model": old["model"]}},
        }
    assert converted["agents"]["default"]["model"] == {
        "primary": {"provider_id": "primary", "model_id": "default"},
        "fallbacks": [{"provider_id": "backup_2", "model_id": "default"},
                      {"provider_id": "backup_1", "model_id": "default"}], "temperature": 0.2,
    }
    backup = Path(report["upgrades"]["configuration"]["backup_root"])
    assert original in [file.read_bytes() for file in backup.glob("file-*.before")]
    after = {file: file.read_bytes() for file in startup_home.rglob("*") if file.is_file()}
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"] == {}
    assert {file: file.read_bytes() for file in startup_home.rglob("*") if file.is_file()} == after


@pytest.mark.parametrize("conflict", ["duplicate", "options", "reference", "current_version", "extra_body"])
def test_initialize_rejects_ambiguous_or_mislabeled_model_config_without_writes(
    startup_home: Path, legacy_model_configuration: dict[str, object], conflict: str,
) -> None:
    providers = legacy_model_configuration["llm"]["providers"]
    if conflict == "duplicate":
        providers.append(providers[0])
    elif conflict == "options":
        providers[0]["request_options"]["parameters"] = {"reasoning_effort": "low"}
    elif conflict == "reference":
        legacy_model_configuration["agents"]["default"]["model"]["primary"] = {
            "provider_id": "backup_1", "model_id": "default",
        }
    elif conflict == "extra_body":
        providers[0]["request_options"]["overrides"]["reasoning_effort"] = "low"
    else:
        legacy_model_configuration["config_version"] = 3
    path = startup_home / "config/workspace.jsonc"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy_model_configuration), encoding="utf-8")
    if conflict == "current_version":
        # 另一个域需要迁移时，也不能把已标记 v3 的非法旧结构当作升级来源。
        (path.parent / "gateway.jsonc").write_text('{"config_version":4}', encoding="utf-8")
    before = {file: file.read_bytes() for file in startup_home.rglob("*") if file.is_file()}
    with pytest.raises(ValueError, match="重复 id|同时声明|配置验证失败|extra_body 重复"):
        main()
    assert {file: file.read_bytes() for file in startup_home.rglob("*") if file.is_file()} == before


def test_initialize_upgrades_old_config_with_exact_backups_and_reenters_without_writes(
    startup_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    config = startup_home / "config"
    config.mkdir(parents=True)
    original = b'// user comment\n{"config_version":1,"logger":{"level":"debug"}}\n'
    path = config / "workspace.jsonc"
    path.write_bytes(original)

    main()
    first = json.loads(capsys.readouterr().out)
    migration = first["upgrades"]["configuration"]
    assert migration["state"] == "completed"
    assert original in [file.read_bytes() for file in Path(migration["backup_root"]).glob("file-*.before")]
    assert read_jsonc_object(path) == {"config_version": 3}
    assert read_jsonc_object(config / "gateway.jsonc")["logging"]["runtime"]["level"] == "debug"
    after = {file: file.read_bytes() for file in config.rglob("*") if file.is_file()}
    main()
    assert json.loads(capsys.readouterr().out)["upgrades"] == {}
    assert {file: file.read_bytes() for file in config.rglob("*") if file.is_file()} == after


@pytest.mark.parametrize("payload,reason", [
    ({"config_version": 999}, "支持范围"),
    ({"config_version": 1, "logger": {"level": "debug"}}, "需要用户决定"),
])
def test_initialize_preserves_unknown_or_conflicting_sources(
    startup_home: Path, payload: dict[str, object], reason: str,
) -> None:
    config = startup_home / "config"
    config.mkdir(parents=True)
    workspace = config / "workspace.jsonc"
    workspace.write_text(json.dumps(payload), encoding="utf-8")
    gateway = config / "gateway.jsonc"
    gateway.write_text('{"config_version":4,"logging":{"runtime":{"level":"info"}}}', encoding="utf-8")
    before = {path: path.read_bytes() for path in config.iterdir()}
    with pytest.raises(ValueError, match=reason):
        main()
    assert {path: path.read_bytes() for path in config.iterdir()} == before


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


def test_running_owner_blocks_layout_before_any_file_changes(
    startup_home: Path, legacy_workspace_payload: dict[str, object],
) -> None:
    config = startup_home / "config"
    config.mkdir(parents=True)
    legacy = config / "boxteam.jsonc"
    original = json.dumps(legacy_workspace_payload).encode()
    legacy.write_bytes(original)
    ownership = SQLiteProcessOwnership(startup_home / "state/gateway/gateway.sqlite")
    ownership.acquire()
    try:
        with pytest.raises(RuntimeError, match="已被另一个进程占用"):
            main()
    finally:
        ownership.release()
    assert list(config.iterdir()) == [legacy]
    assert legacy.read_bytes() == original


def test_live_upgrade_preflight_is_read_only_then_initialization_applies(
    startup_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = startup_home / "config"
    config.mkdir(parents=True)
    path = config / "workspace.jsonc"
    original = b'{"config_version":1,"logger":{"level":"debug"}}\n'
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
    legacy = workspace / ".boxteam/boxteam.jsonc"
    legacy_workspace_payload["logger"] = {"level": "debug"}
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
            "logging_conflict": {"config_version": 4, "logging": {"runtime": {"level": "info"}}},
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
@pytest.mark.parametrize("layout", [True, False])
@pytest.mark.parametrize("fault", ["generation", "route"])
def test_unsettled_identity_is_rejected_before_configuration_or_layout_publish(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch,
    legacy_workspace_payload: dict[str, object], preview: bool, layout: bool, fault: str,
) -> None:
    case = legacy_gateway
    config = case.directory / "home/config"
    config.mkdir(parents=True)
    payload = legacy_workspace_payload if layout else {"config_version": 1, "logger": {"level": "info"}}
    (config / ("boxteam.jsonc" if layout else "workspace.jsonc")).write_text(json.dumps(payload), encoding="utf-8")
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
@pytest.mark.parametrize("layout", [True, False])
def test_unverified_remote_is_rejected_before_configuration_or_layout_publish(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch,
    legacy_workspace_payload: dict[str, object], preview: bool, layout: bool,
) -> None:
    case = legacy_gateway
    config = case.directory / "home/config"
    config.mkdir(parents=True)
    payload = legacy_workspace_payload if layout else {"config_version": 1, "logger": {"level": "info"}}
    (config / ("boxteam.jsonc" if layout else "workspace.jsonc")).write_text(json.dumps(payload), encoding="utf-8")
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


def test_layout_preview_uses_in_memory_candidates_and_creates_no_files(
    startup_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    legacy_workspace_payload: dict[str, object],
) -> None:
    config = startup_home / "config"
    config.mkdir(parents=True)
    legacy = config / "boxteam.jsonc"
    legacy_workspace_payload["logger"] = {"level": "debug"}
    legacy_workspace_payload["gateway"] = {
        "workspaces": [], "ui": {"theme": {"default_theme_id": "warm", "custom_themes": []}},
    }
    runtime = legacy_workspace_payload["runtime"]
    assert isinstance(runtime, dict)
    runtime["auxiliary_services"] = {
        "terminal_backend": {"url": "http://127.0.0.1:8012"},
        "browser_backend": {"url": "http://127.0.0.1:8015"},
    }
    legacy.write_text(json.dumps(legacy_workspace_payload), encoding="utf-8")
    original = legacy.read_bytes()
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--preview"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["upgrade_required"] is True
    assert result["upgrades"] == {"configuration": {"state": "migration_required"},
                                  "layout": {"state": "migration_required"}}
    assert list(startup_home.rglob("*")) == [config, legacy]
    assert legacy.read_bytes() == original


def test_current_workspace_config_with_retired_auxiliary_defaults_triggers_upgrade(
    startup_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    config = startup_home / "config"
    config.mkdir(parents=True)
    workspace = config / "workspace.jsonc"
    original = json.dumps({"config_version": 2, "runtime": {"auxiliary_services": {
        "terminal_backend": {"url": "http://127.0.0.1:8012"},
        "browser_backend": {"url": "http://127.0.0.1:8015"},
    }}}).encode()
    workspace.write_bytes(original)
    main()
    result = json.loads(capsys.readouterr().out)["upgrades"]["configuration"]
    assert result["state"] == "completed"
    assert "auxiliary_services" not in read_jsonc_object(workspace)["runtime"]
    assert original in [path.read_bytes() for path in Path(result["backup_root"]).glob("file-*.before")]


def test_registered_and_explicit_workspace_layout_acquires_one_owner(
    legacy_gateway: LegacyGateway, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    legacy_workspace_payload: dict[str, object],
) -> None:
    case = legacy_gateway
    workspace = case.directory / "workspace-0"
    (workspace / ".boxteam/boxteam.jsonc").write_text(json.dumps(legacy_workspace_payload), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--project-root", str(Path.cwd()),
                                    "--workspace", str(workspace)])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["migrated_workspace_layout"] is True
    assert read_jsonc_object(workspace / ".boxteam/workspace.jsonc")["config_version"] == 3


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
    with pytest.raises(ValueError, match="支持范围"):
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
    (workspace / ".boxteam/boxteam.jsonc").write_text(json.dumps(legacy_workspace_payload), encoding="utf-8")
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
