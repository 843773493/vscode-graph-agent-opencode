"""日志迁移预览必须看到权威快照，且不能替用户应用或改写配置。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from app.core.config_sources import config_revision
from app.gateway.control.gateway_state import GatewayStateStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.cli import main
from configs.runtime_logging_migration import preview_runtime_logging_migration
from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def migration_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "boxteam-home"
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    monkeypatch.delenv("BOXTEAM_GATEWAY_ROOT", raising=False)
    return home


@pytest.fixture
def migration_workspaces(request: pytest.FixtureRequest) -> tuple[Path, Path]:
    output = TestRunContext.from_test_file(Path(request.node.path))
    identity = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:12]
    root = output.workspace_root / identity
    if root.exists():
        shutil.rmtree(root)
    first, second = root / "first", root / "second"
    first.mkdir(parents=True)
    second.mkdir()
    return first, second


@pytest.fixture
def workspace_store(migration_workspaces: tuple[Path, Path]) -> Iterator[WorkspaceStateStore]:
    with WorkspaceStateStore(workspace_root=migration_workspaces[0]) as store:
        yield store


@pytest.fixture
def gateway_store(migration_home: Path) -> Iterator[GatewayStateStore]:
    with GatewayStateStore(path=migration_home / "state/gateway/gateway.sqlite") as store:
        yield store


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// 用户配置原始注释\n" + json.dumps(payload) + "\n", encoding="utf-8")


def _active(store: WorkspaceStateStore | GatewayStateStore, domain: str, payload: dict[str, object]) -> None:
    store.ensure_active_config_snapshot(
        config_domain=domain, payload=payload, source_baseline={}, source_generation=0,
        layer_revisions={}, layer_digests={}, effective_digest=config_revision(payload),
        secret_bindings={}, schema_version=1,
    )


def _database_dump(path: Path) -> str:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        return "\n".join(connection.iterdump())


def _cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr("sys.argv", ["boxteam", "migrate-runtime-logging", *args])
    main()


def test_cli_preview_empty_home_does_not_install_or_create_databases(
    migration_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def reject_write(*_args: object, **_kwargs: object) -> None:
        pytest.fail("预览不能进入可写配置安装或存储初始化")

    monkeypatch.setattr("configs.cli._prepare_storage", reject_write)
    monkeypatch.setattr("configs.cli.install_user_configuration", reject_write)
    monkeypatch.setattr(GatewayStateStore, "__init__", reject_write)
    monkeypatch.setattr(WorkspaceStateStore, "__init__", reject_write)
    _cli(monkeypatch, "--preview")
    report = json.loads(capsys.readouterr().out)
    assert report["validation_stage"] == "logging_preflight"
    assert report["target_config_versions"] == {"gateway": 3, "workspace": 2}
    assert report["settings"] is None
    assert report["conflicts"] == report["unsettled"] == []
    assert all(source["presence"] == "absent" for source in report["sources"])
    assert not migration_home.exists()


def test_all_explicit_layers_are_read_without_scanning_other_workspaces(
    migration_home: Path, migration_workspaces: tuple[Path, Path],
) -> None:
    first, second = migration_workspaces
    paths = [migration_home / "config/workspace.jsonc", migration_home / "config/workspace_local.jsonc",
             first / ".boxteam/workspace.jsonc"]
    for path in paths:
        _write(path, {"logger": {"level": " DEBUG ", "pretty": False}})
    _write(second / ".boxteam/workspace.jsonc", {"logger": {"level": "invalid"}})
    originals = {path: path.read_bytes() for path in paths}

    plan = preview_runtime_logging_migration(boxteam_home=migration_home, workspaces=(first, first))

    assert plan.settings == {"level": "debug", "pretty": False}
    assert plan.conflicts == plan.unsettled == ()
    assert sum(item.path == paths[-1] for item in plan.inputs) == 1
    assert not any(item.path.is_relative_to(second) for item in plan.inputs)
    assert {path: path.read_bytes() for path in paths} == originals
    assert not (first / ".boxteam/state").exists()


def test_conflicts_require_explicit_selection_per_field(
    migration_home: Path, migration_workspaces: tuple[Path, Path],
) -> None:
    first, second = migration_workspaces
    _write(migration_home / "config/gateway.jsonc", {"logging": {"runtime": {"level": "warning", "pretty": True}}})
    _write(first / ".boxteam/workspace.jsonc", {"logger": {"level": "debug", "pretty": False}})
    _write(second / ".boxteam/workspace.jsonc", {"logger": {"level": "info"}})

    plan = preview_runtime_logging_migration(boxteam_home=migration_home, workspaces=(first, second))
    assert {conflict["field"] for conflict in plan.conflicts} == {"level", "pretty"}
    assert plan.settings == {}
    partial = preview_runtime_logging_migration(boxteam_home=migration_home, workspaces=(first, second), level="ERROR")
    assert partial.settings == {"level": "error"}
    assert [conflict["field"] for conflict in partial.conflicts] == ["pretty"]
    selected = preview_runtime_logging_migration(boxteam_home=migration_home, workspaces=(first, second), level="error", pretty=True)
    assert selected.settings == {"level": "error", "pretty": True}
    assert selected.conflicts == ()


def test_new_gateway_layers_keep_their_existing_precedence(migration_home: Path) -> None:
    _write(migration_home / "config/gateway.jsonc", {"config_version": 3, "logging": {"runtime": {"level": "debug"}}})
    _write(migration_home / "config/gateway_local.jsonc", {"logging": {"runtime": {"level": "error"}}})
    report = preview_runtime_logging_migration(boxteam_home=migration_home).report()
    assert report["settings"] is None
    assert report["preserve_gateway_layers"] is True
    assert report["conflicts"] == []


def test_partial_legacy_settings_use_shared_contract_defaults(migration_home: Path) -> None:
    _write(migration_home / "config/workspace.jsonc", {"logger": {"pretty": False}})
    plan = preview_runtime_logging_migration(boxteam_home=migration_home)
    assert plan.settings == {"level": plan.contract.default_level, "pretty": False}


@pytest.mark.parametrize("payload", [
    {"logger": None}, {"logger": {"level": "verbose"}}, {"logger": {"level": 1}},
    {"logger": {"pretty": "false"}}, {"logger": {"pretty": 0}},
    {"logger": {"unknown": True}}, {"logging": {"runtime": {"level": "info"}}},
    {"config_version": 3}, {"config_version": 0}, {"config_version": True}, {"config_version": None},
])
def test_invalid_workspace_logging_is_rejected_even_with_explicit_choices(
    migration_home: Path, payload: dict[str, object],
) -> None:
    path = migration_home / "config/workspace.jsonc"
    _write(path, payload)
    original = path.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        preview_runtime_logging_migration(boxteam_home=migration_home, level="info", pretty=True)
    assert path.read_bytes() == original


@pytest.mark.parametrize("payload", [
    {"logging": None}, {"logging": {"runtime": []}},
    {"logging": {"runtime": {"level": "trace"}}},
    {"logging": {"runtime": {"pretty": 1}}},
    {"logging": {"runtime": {"format": "json"}}}, {"config_version": 4},
])
def test_invalid_gateway_logging_is_rejected(migration_home: Path, payload: dict[str, object]) -> None:
    _write(migration_home / "config/gateway.jsonc", payload)
    with pytest.raises((ValueError, TypeError)):
        preview_runtime_logging_migration(boxteam_home=migration_home)


def test_preview_reads_stored_source_and_active_without_leaking_business_payload(
    migration_home: Path, migration_workspaces: tuple[Path, Path], workspace_store: WorkspaceStateStore,
    gateway_store: GatewayStateStore,
) -> None:
    secret = "test-provider-secret-must-not-appear"
    stored: dict[str, object] = {"logger": {"level": "debug"}, "llm": {"api_key": secret}}
    workspace_store.sync_config_source(
        config_key="workspace_root_mutable_override", uri=None, config_version=1,
        presence="present", payload=stored, layer_digest="frozen-file-byte-digest",
    )
    _active(workspace_store, "workspace", {"logger": {"level": "warning"}, "llm": {"api_key": secret}})
    _active(gateway_store, "gateway", {"config_version": 2, "logging": {"runtime": {"level": "error"}}})
    _write(migration_workspaces[0] / ".boxteam/workspace.jsonc", {"logger": {"level": "info"}})
    before = {store.path: _database_dump(store.path) for store in (workspace_store, gateway_store)}

    plan = preview_runtime_logging_migration(boxteam_home=migration_home, workspaces=(migration_workspaces[0],))
    report = plan.report()

    assert secret not in json.dumps(report)
    assert "llm" not in json.dumps(report)
    assert len(plan.conflicts) == 1
    source = next(item for item in plan.inputs if item.domain == "workspace" and item.kind == "source_layer")
    assert source.digest == "frozen-file-byte-digest"
    assert source.revision == source.config_version == 1
    assert len([item for item in plan.inputs if item.kind == "active"]) == 2
    assert {store.path: _database_dump(store.path) for store in (workspace_store, gateway_store)} == before


@pytest.mark.parametrize("state", ["candidate_validated", "pending_restart", "conflict", "recovery_required"])
def test_unsettled_candidates_claims_and_journals_are_reported_without_mutation(
    migration_home: Path, migration_workspaces: tuple[Path, Path], workspace_store: WorkspaceStateStore, state: str,
) -> None:
    candidate = workspace_store.create_pending_config_candidate(
        config_domain="workspace", candidate_id="candidate", idempotency_key="preview-candidate",
        payload={"logger": {"level": "debug"}}, source_baseline={}, candidate_digest="candidate-digest",
        effective_digest="effective-digest", target_generation=None, fencing_token=None, state="candidate_validated",
    )
    workspace_store.acquire_config_apply_claim(
        config_domain="workspace", candidate_id="candidate", attempt_id="attempt", apply_id="apply",
        owner="preview-test", base_active_revision=None, target_generation=None,
    )
    workspace_store.start_config_apply_journal(
        config_domain="workspace", apply_id="apply", candidate_id="candidate", attempt_id="attempt",
        owner="preview-test", base_active_revision=None, pending_revision=candidate.pending_revision,
        source_baseline={}, active_baseline={},
    )
    # 查询测试允许准备状态；不冒充执行、热更新或恢复验收。
    with closing(workspace_store.connection()) as connection, connection:
        connection.execute("UPDATE config_pending_candidate SET state = ?", (state,))
    before = _database_dump(workspace_store.path)
    plan = preview_runtime_logging_migration(boxteam_home=migration_home, workspaces=(migration_workspaces[0],))
    assert {(item["kind"], item["state"]) for item in plan.unsettled} == {
        ("candidate", state), ("apply", "applying"), ("claim", "claimed"),
    }
    assert _database_dump(workspace_store.path) == before


def test_readonly_preview_observes_committed_wal_without_checkpointing(
    migration_home: Path, migration_workspaces: tuple[Path, Path], workspace_store: WorkspaceStateStore,
) -> None:
    _active(workspace_store, "workspace", {"logger": {"level": "info"}})
    with closing(workspace_store.connection()) as writer:
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        original = workspace_store.path.read_bytes()
        writer.execute("UPDATE config_active_snapshot SET payload_json = ?, effective_digest = ?",
                       (json.dumps({"logger": {"level": "critical"}}), "wal-effective-digest"))
        writer.commit()
        assert workspace_store.path.read_bytes() == original
        wal_path = Path(f"{workspace_store.path}-wal")
        wal = wal_path.read_bytes()
        assert wal
        plan = preview_runtime_logging_migration(boxteam_home=migration_home, workspaces=(migration_workspaces[0],))
        assert plan.settings == {"level": "critical", "pretty": plan.contract.default_pretty}
        assert next(item.digest for item in plan.inputs if item.kind == "active") == "wal-effective-digest"
        assert workspace_store.path.read_bytes() == original
        assert wal_path.read_bytes() == wal


def test_cli_uses_selected_gateway_database_and_reports_legacy_layout(
    migration_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "selected-gateway"
    with GatewayStateStore(path=root / "gateway.sqlite") as store:
        _active(store, "gateway", {"config_version": 2, "logging": {"runtime": {"level": "error"}}})
    _write(migration_home / "config/boxteam.jsonc", {"logger": {"level": "debug"}})
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(root))
    _cli(monkeypatch, "--preview")
    report = json.loads(capsys.readouterr().out)
    assert any(source["source"].startswith(str(root / "gateway.sqlite")) for source in report["sources"])
    assert report["unsettled"][0]["kind"] == "legacy_layout"
    assert not (migration_home / "config/gateway.jsonc").exists()


def test_cli_without_preview_rejects_write_and_preserves_inputs(
    migration_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migration_home / "config/workspace.jsonc"
    _write(path, {"logger": {"level": "debug"}})
    original = path.read_bytes()
    with pytest.raises(SystemExit) as error:
        _cli(monkeypatch)
    assert error.value.code == 2
    assert path.read_bytes() == original
    assert not (migration_home / "state").exists()


def test_cli_accepts_multiple_explicit_workspaces_and_choices(
    migration_home: Path, migration_workspaces: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    first, second = migration_workspaces
    _write(first / ".boxteam/workspace.jsonc", {"logger": {"level": "debug"}})
    _write(second / ".boxteam/workspace.jsonc", {"logger": {"level": "error"}})
    _cli(monkeypatch, "--preview", "--workspace", str(first), "--migration-workspace", str(second),
         "--level", "warning", "--pretty", "false")
    report = json.loads(capsys.readouterr().out)
    assert report["settings"] == report["explicit_choices"] == {"level": "warning", "pretty": False}
    assert report["conflicts"] == []
    assert sum(source["legacy_logger"] for source in report["sources"]) == 2
    assert not migration_home.exists()


def test_sqlite_source_version_is_validated_even_without_payload_version(
    migration_home: Path, migration_workspaces: tuple[Path, Path], workspace_store: WorkspaceStateStore,
) -> None:
    workspace_store.sync_config_source(
        config_key="workspace_mutable_override", uri=None, config_version=3,
        presence="present", payload={"logger": {"level": "info"}}, layer_digest="future-version",
    )
    with pytest.raises(ValueError, match="配置版本不在日志迁移支持范围"):
        preview_runtime_logging_migration(boxteam_home=migration_home, workspaces=(migration_workspaces[0],))


@pytest.mark.parametrize("state", ["committed", "failed", "compensated", "recovery_required"])
def test_completed_journals_remain_history_and_active_recovery_is_reported(
    migration_home: Path, migration_workspaces: tuple[Path, Path], workspace_store: WorkspaceStateStore, state: str,
) -> None:
    _active(workspace_store, "workspace", {"logger": {"level": "info"}})
    workspace_store.start_config_apply_journal(
        config_domain="workspace", apply_id="historical", candidate_id="old", attempt_id="attempt",
        owner="preview-test", base_active_revision=None, pending_revision=None,
        source_baseline={}, active_baseline={},
    )
    workspace_store.update_config_apply_journal(apply_id="historical", expected_state="applying", state=state)
    workspace_store.mark_active_config_snapshot_recovery_required(config_domain="workspace", error="requires original owner")
    before = _database_dump(workspace_store.path)
    plan = preview_runtime_logging_migration(boxteam_home=migration_home, workspaces=(migration_workspaces[0],))
    expected = {("active", "recovery_required")}
    if state == "recovery_required":
        expected.add(("apply", state))
    assert {(item["kind"], item["state"]) for item in plan.unsettled} == expected
    assert _database_dump(workspace_store.path) == before


def test_missing_explicit_workspace_fails_without_creating_it(migration_home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "missing-workspace"
    with pytest.raises(NotADirectoryError, match="显式迁移工作区不存在"):
        preview_runtime_logging_migration(boxteam_home=migration_home, workspaces=(workspace,))
    assert not workspace.exists()
    assert not migration_home.exists()


def test_migration_flags_are_not_silently_accepted_by_other_actions(
    migration_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.argv", ["boxteam", "initialize", "--preview"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert not migration_home.exists()
