"""Saver 公共升级入口的真实非空 artifact、提交故障和跨进程恢复。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.journal import (
    PreparedSchemaV3Upgrade,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.schema_v3_helpers import (
    create_schema2_artifact,
)


@pytest.fixture
def source(request, session_bundle_factory):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session_id = f"entrypoint-{uuid4().hex}"
    session_bundle_factory(sessions, session_id)
    with RolloutCheckpointSaver(sessions) as saver:
        artifact = create_schema2_artifact(saver, session_id)
        yield artifact


def test_public_upgrade_restores_nonempty_assembly_in_new_process(source):
    before = (source.root / "rollout.jsonl").read_bytes()
    original_details = {
        path: path.read_bytes() for path in (source.root / "context-plan-details").rglob("*.json")
    }
    source.saver.upgrade_rollout_schema(source.session_id)
    source.saver.upgrade_rollout_schema(source.session_id)
    assert (source.root / "rollout.jsonl").read_bytes() == before
    assert all(path.read_bytes() == raw for path, raw in original_details.items())
    assert len(tuple((source.root / "schema-upgrade-v3").glob("*/completed.json"))) == 1
    with sqlite3.connect(source.index) as connection:
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (4,)
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE from_version=2 AND to_version=3 AND status='completed'").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE from_version=3 AND to_version=4 AND status='completed'").fetchone() == (1,)
    result = subprocess.run(
        [sys.executable, "-c", """
import json
import sys
from app.domain.itemized.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
with RolloutCheckpointSaver(sys.argv[1]) as saver:
    snapshot = saver.get_context_assembly(sys.argv[2], assembly_id=sys.argv[3])
    snapshot.validate_hashes()
    entries = [entry for entry in snapshot.selection if entry.detail_ref is not None]
    assert entries and all(isinstance(entry.detail_ref, DetailRef) for entry in entries)
    print(json.dumps([saver.read_context_plan_detail(sys.argv[2], detail_ref=entry.detail_ref)['detail'] for entry in entries], ensure_ascii=False))
""", str(source.saver._storage.sessions_dir), source.session_id, source.assembly_id],
        check=False, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout) == [source.body]


def test_public_upgrade_validates_artifacts_before_sql_commit(source, monkeypatch):
    before = (source.root / "rollout.jsonl").read_bytes()
    original = PreparedSchemaV3Upgrade.verify_migrated

    def reject(self, connection):
        original(self, connection)
        assert connection.in_transaction
        assert not (self.audit_root / "completed.json").exists()
        raise RuntimeError("injected-artifact-precommit-failure")

    monkeypatch.setattr(PreparedSchemaV3Upgrade, "verify_migrated", reject)
    with pytest.raises(RuntimeError, match="injected-artifact-precommit-failure"):
        source.saver.upgrade_rollout_schema(source.session_id)
    assert (source.root / "rollout.jsonl").read_bytes() == before
    with sqlite3.connect(source.index) as connection:
        assert connection.execute("SELECT schema_version, database_state FROM database_meta").fetchone() == (2, "recovery_required")
    assert not tuple((source.root / "schema-upgrade-v3").glob("*/completed.json"))
    assert len(tuple((source.root / "schema-upgrade-v3").glob("*/aborted.json"))) == 1


def test_explicit_retry_retains_failed_journal_and_same_published_artifacts(source, monkeypatch):
    before = (source.root / "rollout.jsonl").read_bytes()
    original = PreparedSchemaV3Upgrade.verify_migrated

    def reject(self, connection):
        original(self, connection)
        raise RuntimeError("injected-repeatable-precommit-failure")

    with monkeypatch.context() as patch:
        patch.setattr(PreparedSchemaV3Upgrade, "verify_migrated", reject)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="injected-repeatable-precommit-failure"):
                source.saver.upgrade_rollout_schema(source.session_id)
    with sqlite3.connect(source.index) as connection:
        failed = connection.execute("SELECT * FROM schema_migrations WHERE status='failed' ORDER BY migration_id").fetchall()
        assert len(failed) == 2
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (2, "recovery_required")
    with pytest.raises(RuntimeError, match="schema-upgrade|migration"):
        source.saver.get_context_assembly(source.session_id, assembly_id=source.assembly_id)
    published = {path: path.read_bytes() for path in (source.root / "context-plan-details").rglob("*") if path.is_file()}
    source.saver.upgrade_rollout_schema(source.session_id)
    with sqlite3.connect(source.index) as connection:
        assert connection.execute("SELECT * FROM schema_migrations WHERE status='failed' ORDER BY migration_id").fetchall() == failed
        assert connection.execute("SELECT count(*) FROM schema_migrations WHERE from_version=2 AND to_version=3 AND status='completed'").fetchone() == (1,)
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (4, "active")
    source.saver.get_context_assembly(source.session_id, assembly_id=source.assembly_id).validate_hashes()
    assert all(path.read_bytes() == raw for path, raw in published.items())
    assert (source.root / "rollout.jsonl").read_bytes() == before


@pytest.mark.parametrize("tamper", ["checksum", "name", "from_version", "started", "active"])
def test_explicit_retry_rejects_unmatched_failed_contract(source, monkeypatch, tamper):
    original = PreparedSchemaV3Upgrade.verify_migrated

    def reject(self, connection):
        original(self, connection)
        raise RuntimeError("injected-precommit-failure")

    with monkeypatch.context() as patch:
        patch.setattr(PreparedSchemaV3Upgrade, "verify_migrated", reject)
        with pytest.raises(RuntimeError, match="injected-precommit-failure"):
            source.saver.upgrade_rollout_schema(source.session_id)
    with sqlite3.connect(source.index) as connection:
        statements = {
            "checksum": "UPDATE schema_migrations SET migration_checksum='different-script' WHERE status='failed'",
            "name": "UPDATE schema_migrations SET migration_name='other-migration' WHERE status='failed'",
            "from_version": "UPDATE schema_migrations SET from_version=1 WHERE status='failed'",
            "started": "UPDATE schema_migrations SET status='started' WHERE status='failed'",
            "active": "UPDATE database_meta SET database_state='active'",
        }
        connection.execute(statements[tamper])
    before = (source.root / "rollout.jsonl").read_bytes()
    with pytest.raises(RuntimeError, match="schema-upgrade|未完成"):
        source.saver.upgrade_rollout_schema(source.session_id)
    with sqlite3.connect(source.index) as connection:
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM schema_migrations WHERE from_version=2 AND to_version=3 AND status='completed'").fetchone() == (0,)
    assert (source.root / "rollout.jsonl").read_bytes() == before
    assert not tuple((source.root / "schema-upgrade-v3").glob("*/completed.json"))


def test_explicit_upgrade_cannot_clear_unrelated_recovery_required(source):
    with sqlite3.connect(source.index) as connection:
        connection.execute("UPDATE database_meta SET database_state='recovery_required'")
    with pytest.raises(RuntimeError, match="缺少可认证的失败尝试"):
        source.saver.upgrade_rollout_schema(source.session_id)
    with sqlite3.connect(source.index) as connection:
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (2, "recovery_required")
    assert not (source.root / "schema-upgrade-v3").exists()


def test_public_upgrade_without_protected_key_fails_before_publication(source):
    with sqlite3.connect(source.index) as connection:
        connection.execute("UPDATE context_plan_details SET protection='protected', sensitive=1")
    before = (source.root / "rollout.jsonl").read_bytes()
    with pytest.raises(RuntimeError, match="protected-key-required"):
        source.saver.upgrade_rollout_schema(source.session_id)
    assert (source.root / "rollout.jsonl").read_bytes() == before
    assert not (source.root / "schema-upgrade-v3").exists()
    with sqlite3.connect(source.index) as connection:
        assert connection.execute("SELECT schema_version, database_state FROM database_meta").fetchone() == (2, "active")


@pytest.mark.parametrize("crash_stage", ["before_audit", "after_audit"])
def test_explicit_retry_finishes_committed_audit_before_runtime_reopens(source, crash_stage):
    before = (source.root / "rollout.jsonl").read_bytes()
    result = subprocess.run(
        [sys.executable, "-c", """
import os
import sys
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.migration.schema_v3.journal import PreparedSchemaV3Upgrade
from app.services.infrastructure.rollout_context.storage import schema_upgrade
def crash(*args, **kwargs):
    os._exit(93)
if sys.argv[3] == 'before_audit':
    PreparedSchemaV3Upgrade.verify_committed = crash
else:
    schema_upgrade._activate_verified_artifact_upgrade = crash
RolloutCheckpointSaver(sys.argv[1]).upgrade_rollout_schema(sys.argv[2])
""", str(source.saver._storage.sessions_dir), source.session_id, crash_stage],
        check=False, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 93, (result.stdout, result.stderr)
    with sqlite3.connect(source.index) as connection:
        assert connection.execute("SELECT schema_version, database_state FROM database_meta").fetchone() == (3, "migrating")
    completed = tuple((source.root / "schema-upgrade-v3").glob("*/completed.json"))
    assert bool(completed) == (crash_stage == "after_audit")
    # 普通 reader 不能消费待审计状态，更不能先追加新 Turn 破坏恢复基线。
    with pytest.raises(RuntimeError, match="schema-upgrade-required"):
        source.saver.get_context_assembly(source.session_id, assembly_id=source.assembly_id)
    with pytest.raises(RuntimeError, match="schema-upgrade-required"):
        source.saver.accept_turn(
            source.session_id, accepted_ingress_id="pending-audit-ingress",
            acceptance_idempotency_key="pending-audit-key", payload="不能在审计前写入",
        )
    source.saver.upgrade_rollout_schema(source.session_id)
    assert (source.root / "rollout.jsonl").read_bytes() == before
    with sqlite3.connect(source.index) as connection:
        assert connection.execute("SELECT schema_version, database_state FROM database_meta").fetchone() == (4, "active")
    assert len(tuple((source.root / "schema-upgrade-v3").glob("*/completed.json"))) == 1
    restored = source.saver.get_context_assembly(source.session_id, assembly_id=source.assembly_id)
    restored.validate_hashes()


def test_pending_audit_does_not_activate_changed_typed_artifact(source, monkeypatch):
    before = (source.root / "rollout.jsonl").read_bytes()

    def stop_after_sql(self, connection):
        assert not connection.in_transaction
        raise RuntimeError("injected-before-audit-completion")

    with monkeypatch.context() as patch:
        patch.setattr(PreparedSchemaV3Upgrade, "verify_committed", stop_after_sql)
        with pytest.raises(RuntimeError, match="injected-before-audit-completion"):
            source.saver.upgrade_rollout_schema(source.session_id)
    with sqlite3.connect(source.index) as connection:
        relative = connection.execute("SELECT relative_path FROM context_plan_details LIMIT 1").fetchone()[0]
    (source.root / relative.removeprefix("rollout/")).write_bytes(b'{"corrupt":"typed detail"}')
    with pytest.raises(RuntimeError, match="source-mismatch"):
        source.saver.upgrade_rollout_schema(source.session_id)
    with sqlite3.connect(source.index) as connection:
        assert connection.execute("SELECT schema_version, database_state FROM database_meta").fetchone() == (3, "migrating")
    assert (source.root / "rollout.jsonl").read_bytes() == before
    assert not tuple((source.root / "schema-upgrade-v3").glob("*/completed.json"))
