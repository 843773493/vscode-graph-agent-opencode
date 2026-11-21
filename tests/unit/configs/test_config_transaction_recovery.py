"""启动入口自动结算可证明结束的配置操作，保留待生效正文与原件。"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.config_sources import config_revision
from app.gateway.control.gateway_state import GatewayStateStore
from app.services.infrastructure.config.recovery import plan_stopped_config_recovery
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.maintenance import runtime_logging_commit as publisher
from configs.maintenance.runtime_logging_migration import (
    preview_runtime_logging_migration,
)
from configs.maintenance.startup import prepare_startup_migrations
from configs.runtime import read_jsonc_object


@dataclass
class RecoveryCase:
    home: Path
    workspace: Path
    domain: str
    store: GatewayStateStore | WorkspaceStateStore
    payload: dict[str, object]

    def startup(self, *, preview: bool = False) -> dict[str, object]:
        return prepare_startup_migrations(
            boxteam_home=self.home, project_root=Path.cwd(), gateway_root=self.home / "state/gateway",
            workspaces=(self.workspace,), preview=preview,
        )


@pytest.fixture(params=["gateway", "workspace"])
def recovery_case(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RecoveryCase]:
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    domain = request.param
    store = (GatewayStateStore(path=home / "state/gateway/gateway.sqlite") if domain == "gateway"
             else WorkspaceStateStore(workspace_root=workspace))
    payload = read_jsonc_object(Path.cwd() / f"configs/{domain}_inline.jsonc")
    store.ensure_active_config_snapshot(
        config_domain=domain, payload=payload, source_baseline={}, source_generation=0,
        layer_revisions={}, layer_digests={}, effective_digest=config_revision(payload), secret_bindings={},
        schema_version=payload["config_version"],
    )
    yield RecoveryCase(home, workspace, domain, store, payload)
    store.close()


def _candidate(case: RecoveryCase, *, legacy: bool = False) -> str:
    payload = json.loads(json.dumps(case.payload))
    if legacy:
        if case.domain == "workspace":
            payload["logger"] = {"level": "info"}
        else:
            payload["config_version"] = 4
            payload["features"]["session_generators"] = {}
    case.store.create_pending_config_candidate(
        config_domain=case.domain, candidate_id="candidate", idempotency_key="source-edit",
        payload=payload, source_baseline={}, candidate_digest=config_revision(payload), effective_digest=config_revision(payload),
        target_generation="test-generation", fencing_token="test-token", state="candidate_validated",
        base_active_revision=case.store.get_active_config_snapshot(case.domain).active_revision,
    )
    return "candidate"


def _apply(case: RecoveryCase, *, effect: dict[str, object] | None = None, live_claim: bool = False) -> None:
    candidate_id = _candidate(case)
    if live_claim:
        case.store.acquire_config_apply_claim(
            config_domain=case.domain, candidate_id=candidate_id, attempt_id="attempt", apply_id="apply",
            owner="still-running", base_active_revision=1, target_generation="test-generation", lease_seconds=60,
        )
    case.store.update_pending_config_candidate_state(
        config_domain=case.domain, candidate_id=candidate_id, expected_state="candidate_validated", state="applying",
    )
    case.store.start_config_apply_journal(
        config_domain=case.domain, apply_id="apply", candidate_id=candidate_id, attempt_id="attempt",
        owner="test-config-owner", base_active_revision=1, pending_revision=2, source_baseline={}, active_baseline={},
    )
    if effect is not None:
        case.store.append_config_apply_side_effect(apply_id="apply", side_effect=effect)


def _dump(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return "\n".join(connection.iterdump())


def test_pending_configuration_upgrades_without_applying_or_losing_identity(recovery_case: RecoveryCase) -> None:
    case = recovery_case
    candidate_id = _candidate(case, legacy=True)
    case.store.update_pending_config_candidate_state(
        config_domain=case.domain, candidate_id=candidate_id, expected_state="candidate_validated", state="pending_restart",
    )
    before = _dump(case.store.path)
    preview = preview_runtime_logging_migration(boxteam_home=case.home, workspaces=(case.workspace,))
    assert preview.unsettled == ()
    assert _dump(case.store.path) == before
    case.store.close()
    report = case.startup()
    assert report["configuration"]["state"] == "completed"
    with closing(sqlite3.connect(case.store.path)) as connection:
        row = connection.execute(
            "SELECT candidate_id,state,payload_json,candidate_digest,base_active_revision,pending_revision "
            "FROM config_pending_candidate WHERE candidate_id=?", (candidate_id,),
        ).fetchone()
        active = connection.execute("SELECT active_revision,candidate_id FROM config_active_snapshot").fetchone()
    assert row[0:2] == (candidate_id, "pending_restart")
    assert "logger" not in json.loads(row[2]) and "session_generators" not in json.loads(row[2]).get("features", {})
    assert row[3] == config_revision(json.loads(row[2]))
    assert row[4] == active[0] and row[5] > active[0]
    assert active[1] != candidate_id


def test_startup_recovers_empty_attempt_without_schema_upgrade_and_is_idempotent(recovery_case: RecoveryCase) -> None:
    case = recovery_case
    _apply(case)
    before = _dump(case.store.path)
    case.store.close()
    assert case.startup(preview=True)["configuration"]["state"] == "migration_required"
    assert _dump(case.store.path) == before
    report = case.startup()["configuration"]
    assert report["state"] == "completed"
    assert before in [_dump(path) for path in Path(report["backup_root"]).glob("db-*.before.sqlite")]
    with closing(sqlite3.connect(case.store.path)) as connection:
        assert connection.execute("SELECT state FROM config_apply_journal WHERE apply_id='apply'").fetchone()[0] == "failed"
        assert connection.execute("SELECT state FROM config_pending_candidate WHERE candidate_id='candidate'").fetchone()[0] == "candidate_validated"
    settled = _dump(case.store.path)
    assert case.startup() == {}
    assert _dump(case.store.path) == settled


@pytest.mark.parametrize("effect", [
    {"resource": "unknown-plugin", "action": "applied"},
    {"resource": "runtime-logging", "before": {"pid": os.getpid()}, "current": {"pid": os.getpid()}},
])
def test_unknown_or_live_external_effect_blocks_without_changing_original(recovery_case: RecoveryCase, effect: dict[str, object]) -> None:
    case = recovery_case
    _apply(case, effect=effect)
    before = _dump(case.store.path)
    case.store.close()
    with pytest.raises(ValueError, match="结算未完成操作"):
        case.startup()
    assert _dump(case.store.path) == before
    assert not (case.home / "config/runtime-logging-migrations").exists()


def test_live_claim_cannot_be_taken_by_startup_or_online_recovery(recovery_case: RecoveryCase) -> None:
    case = recovery_case
    _apply(case, live_claim=True)
    assert case.store.recover_expired_config_applies(config_domain=case.domain) == ()
    with closing(case.store.connection()) as connection:
        assert plan_stopped_config_recovery(connection, case.domain).blockers


def test_stopped_logging_effect_is_settled_without_fabricating_success(
    recovery_case: RecoveryCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = recovery_case
    _apply(case, effect={"resource": "runtime-logging", "action": "applied", "before": {"pid": 12345}, "current": {"pid": 12345}})
    monkeypatch.setattr("app.services.infrastructure.config.recovery.psutil.pid_exists", lambda pid: False)
    case.store.close()
    assert case.startup()["configuration"]["state"] == "completed"
    with closing(sqlite3.connect(case.store.path)) as connection:
        assert connection.execute("SELECT state FROM config_apply_journal WHERE apply_id='apply'").fetchone()[0] == "failed"


def test_newer_active_supersedes_old_candidate_and_expired_claim(recovery_case: RecoveryCase) -> None:
    case = recovery_case
    _apply(case, live_claim=True)
    # 模拟提交新 active 后、旧尝试 finally 前退出；同一 DB 原子提交状态，不伪造外部副作用。
    with closing(case.store.connection()) as connection, connection:
        connection.execute("UPDATE config_apply_claim SET lease_expires_at='2000-01-01T00:00:00+00:00'")
        connection.execute("UPDATE config_active_snapshot SET active_revision=3")
    case.store.close()
    assert case.startup()["configuration"]["state"] == "completed"
    with closing(sqlite3.connect(case.store.path)) as connection:
        assert connection.execute("SELECT state FROM config_pending_candidate WHERE candidate_id='candidate'").fetchone()[0] == "discarded"
        assert connection.execute("SELECT COUNT(*) FROM config_apply_claim").fetchone()[0] == 0


def test_committed_active_proof_finishes_interrupted_bookkeeping(recovery_case: RecoveryCase) -> None:
    case = recovery_case
    _apply(case)
    with closing(case.store.connection()) as connection, connection:
        connection.execute("UPDATE config_active_snapshot SET candidate_id='candidate',promoted_apply_id='apply',active_revision=2")
    case.store.close()
    assert case.startup()["configuration"]["state"] == "completed"
    with closing(sqlite3.connect(case.store.path)) as connection:
        assert connection.execute("SELECT state FROM config_apply_journal WHERE apply_id='apply'").fetchone()[0] == "committed"
        assert connection.execute("SELECT state FROM config_pending_candidate WHERE candidate_id='candidate'").fetchone()[0] == "active"


def test_recovery_publication_interruption_replays_same_plan(recovery_case: RecoveryCase, monkeypatch: pytest.MonkeyPatch) -> None:
    case = recovery_case
    _apply(case)
    case.store.close()
    original = publisher.replace_configuration_rows

    def interrupt(connection: sqlite3.Connection, source: sqlite3.Connection) -> None:
        original(connection, source)
        raise RuntimeError("模拟数据库发布中断")

    # _publish 默认参数固定了 owner，替换原函数的默认依赖来模拟事务内中断。
    defaults = publisher._publish.__kwdefaults__.copy()
    monkeypatch.setattr(publisher._publish, "__kwdefaults__", {**defaults, "replace_database": interrupt})
    with pytest.raises(RuntimeError, match="模拟数据库发布中断"):
        case.startup()
    monkeypatch.setattr(publisher._publish, "__kwdefaults__", defaults)
    assert case.startup()["configuration"]["state"] == "completed"
    with closing(sqlite3.connect(case.store.path)) as connection:
        assert connection.execute("SELECT state FROM config_apply_journal WHERE apply_id='apply'").fetchone()[0] == "failed"
