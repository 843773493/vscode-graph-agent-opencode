"""日志迁移预览必须看到权威快照，且不能替用户应用或改写配置。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from app.core.config_sources import config_revision
from app.gateway.control.gateway_state import GatewayStateStore
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.cli import main
from configs.runtime import read_jsonc_object
from configs.runtime_logging_commit import commit_runtime_logging_migration
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
    assert report["target_config_versions"] == {"gateway": 4, "workspace": 2}
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
    {"logging": {"runtime": {"format": "json"}}}, {"config_version": 5},
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


def test_cli_migrates_and_backs_up_original_bytes(
    migration_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    path = migration_home / "config/workspace.jsonc"
    _write(path, {"logger": {"level": "debug"}})
    original = path.read_bytes()
    _cli(monkeypatch)
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "completed"
    assert "logger" not in json.loads(path.read_text())
    assert any(item.read_bytes() == original for item in Path(report["backup_root"]).glob("file-*.before"))
    assert json.loads((migration_home / "config/gateway.jsonc").read_text())["logging"]["runtime"] == {"level": "debug", "pretty": True}
    assert not (migration_home / "state/gateway/gateway.sqlite").exists()


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


def _legacy_payload() -> dict[str, object]:
    payload = read_jsonc_object(Path.cwd() / "configs/workspace_inline.jsonc")
    return {**payload, "config_version": 1, "logger": {"level": "info", "pretty": True}}


def test_migration_preserves_active_business_state_and_source_cas(
    migration_home: Path, migration_workspaces: tuple[Path, Path], workspace_store: WorkspaceStateStore,
) -> None:
    from app.services.infrastructure.config_service import ConfigService

    first = migration_workspaces[0]
    path = first / ".boxteam/workspace.jsonc"
    active = _legacy_payload()
    _write(path, active)
    original_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    source = workspace_store.sync_config_source(
        config_key="workspace_root_mutable_override", uri=None, config_version=1,
        presence="present", payload=active, layer_digest=original_digest,
    )
    baseline: dict[str, object] = {source.config_key: {"uri": None, "presence": "present", "layer_revision": source.layer_revision,
                                  "layer_digest": source.layer_digest, "source_generation": source.source_generation}}
    original = workspace_store.ensure_active_config_snapshot(
        config_domain="workspace", payload=active, source_baseline=baseline, source_generation=source.source_generation,
        layer_revisions={source.config_key: source.layer_revision}, layer_digests={source.config_key: source.layer_digest},
        effective_digest=config_revision(active), secret_bindings={}, schema_version=1,
    )
    changed = {**active, "development": {"test_tools": True}}
    _write(path, changed)
    before = _database_dump(workspace_store.path)
    workspace_store.close()

    result = commit_runtime_logging_migration(boxteam_home=migration_home, workspaces=(first,))
    assert result["state"] == "completed"
    with WorkspaceStateStore(workspace_root=first) as restored:
        migrated = restored.get_active_config_snapshot("workspace")
        assert migrated is not None
        assert migrated.payload == {key: (2 if key == "config_version" else value) for key, value in active.items() if key != "logger"}
        assert migrated.active_revision > original.active_revision
        assert migrated.effective_digest == config_revision(migrated.payload)
        assert migrated.schema_version == 2
        layer = restored.get_source_layer(source.config_key)
        assert layer is not None
        assert layer.layer_revision == source.layer_revision + 1
        assert migrated.layer_revisions[source.config_key] == layer.layer_revision
        assert migrated.layer_digests[source.config_key] == layer.layer_digest
        service = ConfigService(config_dir=Path.cwd() / "configs", workspace_root=first, workspace_state_store=restored)
        assert service.get_snapshot().to_dict()["development"]["test_tools"] is False
        assert json.loads(path.read_text())["development"]["test_tools"] is True
    after = _database_dump(workspace_store.path)
    backup_root = Path(str(result["backup_root"]))
    backup = next(backup_root.glob("db-*.before.sqlite"))
    assert _database_dump(backup) == before
    repeated = commit_runtime_logging_migration(boxteam_home=migration_home, workspaces=(first,))
    assert repeated["state"] == "unchanged"
    assert _database_dump(workspace_store.path) == after
    # 产品重新启动后会正常写来源观察，原件恢复不能覆盖这些后续事实。
    with pytest.raises(RuntimeError, match="配置被外部修改"):
        commit_runtime_logging_migration(boxteam_home=migration_home, restore=True)
    assert _database_dump(workspace_store.path) == after
    assert json.loads(path.read_text()) == {key: (2 if key == "config_version" else value) for key, value in changed.items() if key != "logger"}


def test_restore_database_before_restart_returns_exact_original(
    migration_home: Path, migration_workspaces: tuple[Path, Path], workspace_store: WorkspaceStateStore,
) -> None:
    _active(workspace_store, "workspace", _legacy_payload())
    before = _database_dump(workspace_store.path)
    workspace_store.close()
    commit_runtime_logging_migration(boxteam_home=migration_home, workspaces=(migration_workspaces[0],))
    assert commit_runtime_logging_migration(boxteam_home=migration_home, restore=True)["state"] == "restored"
    assert _database_dump(workspace_store.path) == before


@pytest.mark.parametrize("restore", [False, True])
def test_interrupted_migration_blocks_runtime_and_can_resume_or_restore(
    migration_home: Path, migration_workspaces: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, restore: bool,
) -> None:
    import configs.runtime_logging_commit as commit

    first = migration_workspaces[0]
    path = first / ".boxteam/workspace.jsonc"
    _write(path, {"config_version": 1, "logger": {"level": "debug"}})
    original = path.read_bytes()
    write = commit.atomic_write

    def interrupted(target: Path, content: bytes, mode: int) -> None:
        if target == path:
            raise OSError("注入发布中断")
        write(target, content, mode)

    with monkeypatch.context() as patch:
        patch.setattr(commit, "atomic_write", interrupted)
        with pytest.raises(OSError, match="注入发布中断"):
            commit_runtime_logging_migration(boxteam_home=migration_home, workspaces=(first,))
    with pytest.raises(RuntimeError, match="禁止读取部分迁移状态"):
        read_jsonc_object(migration_home / "config/gateway.jsonc")
    with pytest.raises(RuntimeError, match="禁止读取部分迁移状态"):
        WorkspaceStateStore(workspace_root=first)
    with pytest.raises(ValueError, match="不能更换日志选择"):
        commit_runtime_logging_migration(boxteam_home=migration_home, level="error")
    assert commit_runtime_logging_migration(boxteam_home=migration_home, level="debug", pretty=True, restore=restore)["state"] == ("restored" if restore else "completed")
    if restore:
        assert path.read_bytes() == original
        assert not (migration_home / "config/gateway.jsonc").exists()
    else:
        assert json.loads(path.read_text()) == {"config_version": 2}
        assert read_jsonc_object(migration_home / "config/gateway.jsonc")["logging"] == {"runtime": {"level": "debug", "pretty": True}}


def test_live_owner_blocks_migration_before_publishing_any_configuration(
    migration_home: Path, migration_workspaces: tuple[Path, Path], workspace_store: WorkspaceStateStore,
) -> None:
    first = migration_workspaces[0]
    path = first / ".boxteam/workspace.jsonc"
    _write(path, {"logger": {"level": "info"}})
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="已被另一个进程占用"):
        commit_runtime_logging_migration(boxteam_home=migration_home, workspaces=(first,))
    assert path.read_bytes() == before
    assert not (migration_home / "config/gateway.jsonc").exists()


def test_unmigrated_workspace_is_rejected_before_writing_legacy_source(
    migration_workspaces: tuple[Path, Path], workspace_store: WorkspaceStateStore,
) -> None:
    from app.services.infrastructure.config_service import ConfigService

    path = migration_workspaces[0] / ".boxteam/workspace.jsonc"
    _write(path, {"logger": {"level": "info"}})
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=path, workspace_state_store=workspace_store)
    with pytest.raises(ValueError, match="migrate-runtime-logging"):
        service.get_snapshot()
    assert workspace_store.get_source_layer("workspace_mutable_override") is None


@pytest.mark.parametrize("restore", [False, True])
def test_interrupt_after_database_commit_retains_recoverable_owner_boundary(
    migration_home: Path, migration_workspaces: tuple[Path, Path], workspace_store: WorkspaceStateStore,
    monkeypatch: pytest.MonkeyPatch, restore: bool,
) -> None:
    import configs.runtime_logging_commit as commit

    first = migration_workspaces[0]
    _active(workspace_store, "workspace", _legacy_payload())
    before = _database_dump(workspace_store.path)
    workspace_store.close()
    write = commit.atomic_write

    def interrupted(path: Path, content: bytes, mode: int) -> None:
        if path == migration_home / "config/workspace_schema.jsonc":
            raise OSError("注入数据库发布后中断")
        write(path, content, mode)

    with monkeypatch.context() as patch:
        patch.setattr(commit, "atomic_write", interrupted)
        with pytest.raises(OSError, match="数据库发布后中断"):
            commit_runtime_logging_migration(boxteam_home=migration_home, workspaces=(first,))
    with pytest.raises(RuntimeError, match="禁止读取部分迁移状态"):
        WorkspaceStateStore(workspace_root=first)
    commit_runtime_logging_migration(boxteam_home=migration_home, restore=restore)
    if restore:
        assert _database_dump(workspace_store.path) == before
    else:
        with WorkspaceStateStore(workspace_root=first) as store:
            active = store.get_active_config_snapshot("workspace")
            assert active is not None and active.schema_version == 2 and "logger" not in active.payload


@pytest.mark.parametrize("registered", [False, True])
def test_gateway_connection_ids_are_frozen_once_and_reuse_existing_identity(
    migration_home: Path, gateway_store: GatewayStateStore, registered: bool,
) -> None:
    path = migration_home / "config/gateway.jsonc"
    connection = {"kind": "remote_gateway", "host": "example.test", "username": "developer", "private_key_path": "~/.ssh/key"}
    _write(path, {"config_version": 1, "workspaces": [connection]})
    if registered:
        gateway_store.set_config(config_key="gateway_connection_ids", config_version=4, payload={"entries": [
            {"connection_id": "connection_original", "fingerprint": ["example.test", 22, "developer", "~/.ssh/key", None, 8014]},
        ]})
    gateway_store.close()
    commit_runtime_logging_migration(boxteam_home=migration_home)
    migrated = json.loads(path.read_text())
    identity = migrated["workspaces"][0]["connection_id"]
    assert identity
    if registered:
        assert identity == "connection_original"
    assert migrated["config_version"] == 4
    assert commit_runtime_logging_migration(boxteam_home=migration_home)["state"] == "unchanged"
    assert json.loads(path.read_text())["workspaces"][0]["connection_id"] == identity


def test_shared_source_pending_fanout_blocks_explicit_migration(migration_home: Path) -> None:
    from app.services.infrastructure.config.source_owner import WorkspaceSourceOwner

    owner = WorkspaceSourceOwner(path=migration_home / "config/workspace-source.sqlite")
    owner.observe(uri=None, presence="present", layer_digest="old", origin="test")
    owner.prepare_fanout(workspace_id="workspace_awaiting", after_generation=0)
    report = preview_runtime_logging_migration(boxteam_home=migration_home).report()
    unsettled = cast(list[dict[str, str]], report["unsettled"])
    assert unsettled[0]["kind"] == "source_fanout"
    with pytest.raises(ValueError, match="未结算配置"):
        commit_runtime_logging_migration(boxteam_home=migration_home)
    assert not (migration_home / "config/gateway.jsonc").exists()


@pytest.mark.asyncio
async def test_shared_source_owner_resumes_existing_generation_after_migration(
    migration_home: Path, migration_workspaces: tuple[Path, Path], workspace_store: WorkspaceStateStore,
) -> None:
    from app.core.workspace_identity import load_or_create_workspace_id
    from app.services.infrastructure.config.source_owner import WorkspaceSourceOwner
    from app.services.infrastructure.config_service import ConfigService

    first = migration_workspaces[0]
    workspace_id = load_or_create_workspace_id(first)
    path = migration_home / "config/workspace.jsonc"
    payload = _legacy_payload()
    _write(path, payload)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    layer = workspace_store.sync_config_source(config_key="workspace_mutable_override", uri=None,
        config_version=1, presence="present", payload=payload, layer_digest=digest)
    _active(workspace_store, "workspace", payload)
    owner = WorkspaceSourceOwner(path=migration_home / "config/workspace-source.sqlite")
    observed = owner.observe(uri=None, presence="present", layer_digest=digest, origin="historical-runtime")
    owner.prepare_fanout(workspace_id=workspace_id, after_generation=0)
    owner.record_fanout(source_generation=observed.source_generation, workspace_id=workspace_id,
        status="applied", layer_revision=layer.layer_revision, layer_digest=digest)
    history = owner.list_journal()
    workspace_store.close()
    commit_runtime_logging_migration(boxteam_home=migration_home, workspaces=(first,))
    assert owner.list_journal() == history
    with WorkspaceStateStore(workspace_root=first) as store:
        service = ConfigService(config_dir=Path.cwd() / "configs", workspace_root=first, config_path=path,
            workspace_state_store=store, source_owner=owner, source_owner_workspace_id=workspace_id)
        service.validate_workspace_config()
        assert await service.reload() is False
        current = store.get_source_layer("workspace_mutable_override")
        assert current is not None and current.source_generation == observed.source_generation + 1
        assert owner.list_journal()[0] == history[0]
        assert owner.list_journal()[-1].layer_digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_gateway_v3_migration_retires_generator_config_with_backup(migration_home: Path) -> None:
    path = migration_home / "config/gateway.jsonc"
    _write(path, {"config_version": 3, "features": {"session_generators": {"poll_interval_seconds": 3}},
                  "logging": {"runtime": {"level": "debug", "pretty": False}}})
    original = path.read_bytes()
    preview = preview_runtime_logging_migration(boxteam_home=migration_home).report()
    assert preview["target_config_versions"]["gateway"] == 4
    assert path.read_bytes() == original
    result = commit_runtime_logging_migration(boxteam_home=migration_home)
    migrated = json.loads(path.read_text())
    assert migrated["config_version"] == 4 and "session_generators" not in migrated["features"]
    assert migrated["logging"]["runtime"] == {"level": "debug", "pretty": False}
    assert result["state"] == "completed"
    assert commit_runtime_logging_migration(boxteam_home=migration_home)["state"] == "unchanged"
