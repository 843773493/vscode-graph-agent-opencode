"""冻结真实 schema3 DDL 的显式 Saver 升级、拒绝、事务回滚与重启验收。"""

from __future__ import annotations

import builtins
import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.migration.schema_v4 import (
    MIGRATION_NAME,
    PreparedSchemaV4Upgrade,
    prepare_schema_v4_upgrade,
)
from app.services.infrastructure.rollout_context.migration.schema_v4.model import (
    fingerprint,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.schema_v4_helpers import (
    bind_frozen_omitted_source,
    create_schema3_artifact,
    immutable_files,
)


@pytest.fixture
def source(request, session_bundle_factory):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session_id = f"schema3-{uuid4().hex}"
    session_bundle_factory(sessions, session_id)
    with RolloutCheckpointSaver(sessions) as saver:
        yield create_schema3_artifact(saver, session_id, **getattr(request, "param", {}))


def prepare(source):
    with closing(sqlite3.connect(source.index.as_uri() + "?mode=ro", uri=True)) as connection:
        return prepare_schema_v4_upgrade(connection, session_id=source.session_id, checkpoint_ns="")


def test_public_upgrade_preserves_sealed_facts_and_explicit_import_origin(source):
    files = immutable_files(source.root)
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (3,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='context_plans'").fetchone() is None
        assert next(row[3] for row in connection.execute("PRAGMA table_info(tool_set_snapshots)") if row[1] == "assembly_id") == 1
        assert connection.execute("PRAGMA foreign_key_list(tool_set_snapshots)").fetchall() == []
        snapshot_raw = connection.execute("SELECT snapshot_json FROM context_assemblies").fetchone()[0]
        tools = connection.execute("SELECT * FROM tool_set_snapshots").fetchall()
    with pytest.raises(RuntimeError, match="schema-upgrade-required"):
        source.saver.get_context_assembly(source.session_id, assembly_id=source.assembly_id)
    source.saver.upgrade_rollout_schema(source.session_id)
    source.saver.upgrade_rollout_schema(source.session_id)
    assert immutable_files(source.root) == files
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (4, "active")
        assert connection.execute("SELECT snapshot_json FROM context_assemblies").fetchone() == (snapshot_raw,)
        assert connection.execute("SELECT * FROM tool_set_snapshots").fetchall() == tools
        row = connection.execute("SELECT registration_origin,source_provenance_json,plan_creation_idempotency_key,creation_hash,creation_json,draft_json,draft_hash,revision,plan_state FROM context_plans").fetchone()
        assert row[0] == "schema3_import" and row[2:] == (None, None, None, None, None, 0, "sealed")
        assert json.loads(row[1]) == {
            "source_session_id": source.session_id, "source_plan_id": source.plan_id,
            "source_assembly_id": source.assembly_id, "source_schema_version": 3,
            "source_snapshot_hash": "sha256:jcs:v1:" + hashlib.sha256(snapshot_raw.encode()).hexdigest(),
            "audit_id": json.loads(row[1])["audit_id"],
        }
        source_manifest = json.loads(connection.execute("SELECT source_manifest_json FROM context_plans").fetchone()[0])
        assert source_manifest["schema"] == "context-plan-source:v1"
        assert (source_manifest["session_id"], source_manifest["plan_id"]) == (source.session_id, source.plan_id)
        assert source_manifest["refs"] == json.loads(snapshot_raw)["refs"]
        assert len(source_manifest["contributions"]) == 1
        assert all(item["body"] is None and item["assembly_id"] is None and item["contribution_ordinal"] is None
                   for item in source_manifest["contributions"])
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE from_version=3 AND to_version=4 AND status='completed'").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM context_plan_seal_failures").fetchone() == (0,)
        assert next(row[3] for row in connection.execute("PRAGMA table_info(tool_set_snapshots)") if row[1] == "assembly_id") == 0
    restored = source.saver.get_context_plan_registration(source.session_id, plan_id=source.plan_id)
    assert restored.draft is None and restored.creation_hash is None
    snapshot = source.saver.get_context_assembly(source.session_id, assembly_id=source.assembly_id)
    assert snapshot.tool_set_refs[0].tools[0]["description"] == "工具正文必须保留"
    detail = next(entry.detail_ref for entry in snapshot.selection if entry.detail_ref is not None)
    assert source.saver.read_context_plan_detail(source.session_id, detail_ref=detail)["detail"] == source.body


def test_prepare_is_read_only_and_sql_is_deterministic(source):
    before = source.index.read_bytes()
    files = immutable_files(source.root)
    with closing(sqlite3.connect(source.index.as_uri() + "?mode=ro", uri=True)) as connection:
        changes = connection.total_changes
        first = prepare_schema_v4_upgrade(connection, session_id=source.session_id, checkpoint_ns="")
        second = prepare_schema_v4_upgrade(connection, session_id=source.session_id, checkpoint_ns="")
        assert connection.total_changes == changes == 0
        assert not connection.in_transaction
    assert first.migration_sql == second.migration_sql
    assert first.audit_id == second.audit_id
    assert "schema4-plan-import" in first.migration_sql
    assert source.index.read_bytes() == before
    assert immutable_files(source.root) == files


@pytest.mark.parametrize("statement", [
    "DELETE FROM tool_set_snapshots",
    "UPDATE tool_set_snapshots SET tools_json='[]'",
    "DELETE FROM assembly_item_refs WHERE ref_type='request_only'",
    "DELETE FROM context_assembly_contributions",
    "UPDATE context_assembly_selections SET content_length=999 WHERE included=1",
    "UPDATE context_plan_details SET content_length=999",
    "UPDATE context_assemblies SET request_hash='wrong'",
    "UPDATE storage_commits SET status='pending' WHERE commit_kind='assembly_sealed'",
])
def test_corrupt_source_is_rejected_without_repair(source, statement):
    with sqlite3.connect(source.index) as connection:
        connection.execute(statement)
    before = source.index.read_bytes()
    files = immutable_files(source.root)
    with pytest.raises((RuntimeError, ValueError), match="source-mismatch|detail-unavailable|commit"):
        source.saver.upgrade_rollout_schema(source.session_id)
    assert source.index.read_bytes() == before
    assert immutable_files(source.root) == files


def test_duplicate_plan_binding_is_rejected_and_both_rows_survive(source):
    with sqlite3.connect(source.index) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(context_assemblies)")]
        values = list(connection.execute("SELECT * FROM context_assemblies").fetchone())
        values[columns.index("assembly_id")] = "duplicate-assembly"
        connection.execute(f"INSERT INTO context_assemblies VALUES({','.join('?' for _ in values)})", values)
    before = source.index.read_bytes()
    with pytest.raises(RuntimeError, match="schema-upgrade-plan-collision"):
        prepare(source)
    assert source.index.read_bytes() == before
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT count(*) FROM context_assemblies").fetchone() == (2,)


def test_precommit_failure_rolls_back_and_retry_keeps_failed_journal(source, monkeypatch):
    first = prepare(source)
    files = immutable_files(source.root)
    with closing(sqlite3.connect(source.index)) as connection:
        original_fingerprint = fingerprint(connection)
    original_verify = PreparedSchemaV4Upgrade.verify_migrated

    def fail(self, connection):
        before = connection.total_changes
        original_verify(self, connection)
        assert connection.in_transaction and connection.total_changes == before
        raise RuntimeError("injected-schema4-precommit-failure")

    with monkeypatch.context() as patch:
        patch.setattr(PreparedSchemaV4Upgrade, "verify_migrated", fail)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="injected-schema4-precommit-failure"):
                source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (3, "recovery_required")
        assert fingerprint(connection) == original_fingerprint
        failed = connection.execute("SELECT * FROM schema_migrations WHERE status='failed' ORDER BY migration_id").fetchall()
        assert len(failed) == 2
    assert prepare(source).migration_sql == first.migration_sql
    source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT * FROM schema_migrations WHERE status='failed' ORDER BY migration_id").fetchall() == failed
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (4, "active")
    assert immutable_files(source.root) == files


def test_committed_upgrade_restores_in_fresh_process(source):
    source.saver.upgrade_rollout_schema(source.session_id)
    result = subprocess.run([sys.executable, "-c", """
import sys
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
with RolloutCheckpointSaver(sys.argv[1]) as saver:
    saver.upgrade_rollout_schema(sys.argv[2])
    plan = saver.get_context_plan_registration(sys.argv[2], plan_id=sys.argv[3])
    assert plan.draft is None and plan.registration_origin == 'schema3_import'
    snapshot = saver.get_context_assembly(sys.argv[2], assembly_id=plan.assembly_id)
    snapshot.validate_hashes()
    ref = next(entry.detail_ref for entry in snapshot.selection if entry.detail_ref is not None)
    assert saver.read_context_plan_detail(sys.argv[2], detail_ref=ref)['detail']
""", str(source.saver._storage.sessions_dir), source.session_id, source.plan_id], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_migration_name_matches_storage_port():
    assert MIGRATION_NAME == "v4_context_plan_registry"


@pytest.mark.parametrize("statement", [
    "UPDATE database_meta SET schema_version=2",
    "UPDATE database_meta SET schema_version=4",
    "UPDATE database_meta SET rollout_format_version=1",
    "UPDATE database_meta SET session_id='other-session'",
    "UPDATE database_meta SET database_state='migrating'",
    "UPDATE database_meta SET database_state='recovery_required'",
    "CREATE TABLE context_plans(partial TEXT)",
])
def test_manifest_and_partial_install_are_not_recovered_by_initialization(source, statement):
    with sqlite3.connect(source.index) as connection:
        connection.execute(statement)
    before = source.index.read_bytes()
    with pytest.raises(RuntimeError, match="schema-upgrade|source-mismatch|失败尝试"):
        prepare(source)
    assert source.index.read_bytes() == before


def test_prepare_rejects_uncommitted_source_and_unknown_namespace(source):
    with closing(sqlite3.connect(source.index)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="transaction-conflict"):
            prepare_schema_v4_upgrade(connection, session_id=source.session_id, checkpoint_ns="")
        connection.rollback()
        with pytest.raises(RuntimeError, match="namespace 未注册"):
            prepare_schema_v4_upgrade(connection, session_id=source.session_id, checkpoint_ns="unregistered")


@pytest.mark.parametrize("statement", [
    "UPDATE context_plans SET source_provenance_json='{}'",
    "UPDATE context_plans SET source_manifest_json='{}'",
    "UPDATE context_plans SET seal_hash='wrong'",
    "UPDATE context_plans SET created_at='fake-history'",
    "UPDATE item_catalog SET jsonl_offset=999",
    "UPDATE context_assemblies SET snapshot_json='{}'",
    "DELETE FROM context_plan_refs",
    "UPDATE tool_set_snapshots SET assembly_id=NULL",
])
def test_precommit_validator_rejects_changed_old_facts_and_import_bindings(source, monkeypatch, statement):
    files = immutable_files(source.root)
    original_verify = PreparedSchemaV4Upgrade.verify_migrated
    with closing(sqlite3.connect(source.index)) as connection:
        original_fingerprint = fingerprint(connection)

    def tamper(self, connection):
        connection.execute(statement)
        original_verify(self, connection)

    monkeypatch.setattr(PreparedSchemaV4Upgrade, "verify_migrated", tamper)
    with pytest.raises(RuntimeError, match="source-mismatch"):
        source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (3, "recovery_required")
        assert fingerprint(connection) == original_fingerprint
    assert immutable_files(source.root) == files


@pytest.mark.parametrize("source", [{"empty": True}], indirect=True)
def test_schema3_without_assemblies_builds_empty_registry(source):
    before = immutable_files(source.root)
    source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (4,)
        for table in ("context_plans", "context_plan_refs", "context_plan_contributions", "context_plan_seal_failures"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    assert immutable_files(source.root) == before


@pytest.mark.parametrize("source", [{"omit_sources": True}], indirect=True)
@pytest.mark.parametrize("known_mapping", [False, True])
def test_import_preserves_legitimate_omission_without_tool_or_detail_backfill(source, monkeypatch, known_mapping):
    if known_mapping:
        bind_frozen_omitted_source(source, "schema3-contribution")
    before = immutable_files(source.root)
    with closing(sqlite3.connect(source.index)) as connection:
        raw = connection.execute("SELECT snapshot_json FROM context_assemblies").fetchone()[0]
        assert connection.execute("SELECT COUNT(*) FROM tool_set_snapshots").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM context_assembly_contributions").fetchone() == (0,)
        assert connection.execute("SELECT contribution_id FROM context_contributions").fetchall() == [("schema3-contribution",)]

    def forbidden_body(*args, **kwargs):
        raise AssertionError("SQL-only upgrade 不得读取或补造 source body")

    monkeypatch.setattr(source.saver._detail_store, "read", forbidden_body)
    monkeypatch.setattr(source.saver, "_recover_request_source", forbidden_body)
    source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT snapshot_json FROM context_assemblies").fetchone() == (raw,)
        for table in ("tool_set_snapshots", "context_plan_details", "context_assembly_contributions"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        source_manifest = json.loads(connection.execute("SELECT source_manifest_json FROM context_plans").fetchone()[0])
        assert source_manifest["refs"] == json.loads(raw)["refs"]
        contributions = source_manifest["contributions"]
        assert len(contributions) == 1 and contributions[0]["contribution_id"] == "schema3-contribution"
        assert contributions[0]["body"] is None and contributions[0]["assembly_id"] is None
        assert contributions[0]["contribution_ordinal"] is None and "detail_ref" not in contributions[0]
        assert connection.execute("SELECT contribution_id,manifest_json FROM context_plan_contributions").fetchall() == [
            ("schema3-contribution", canonical_json_bytes(contributions[0]).decode())
        ]
    snapshot = source.saver.get_context_assembly(source.session_id, assembly_id=source.assembly_id)
    omitted = [entry for entry in snapshot.selection if not entry.included]
    assert len(omitted) == 2
    assert all(entry.omission_reason and entry.loss and entry.detail_ref is None for entry in omitted)
    assert {entry.ref.ref_type for entry in omitted} == {"request_only", "tool_set"}
    assert next(entry.contribution_id for entry in omitted if entry.ref.ref_type == "request_only") == (
        "schema3-contribution" if known_mapping else None
    )
    assert immutable_files(source.root) == before


@pytest.mark.parametrize("source", [{"omit_sources": True}], indirect=True)
@pytest.mark.parametrize("corruption", ["unknown_id", "missing_registry", "different_registry", "changed_revision"])
def test_omitted_mapping_requires_authentic_frozen_registry_source(source, corruption):
    bind_frozen_omitted_source(source, "invented-contribution" if corruption == "unknown_id" else "schema3-contribution")
    with closing(sqlite3.connect(source.index)) as connection:
        if corruption == "missing_registry":
            connection.execute("DELETE FROM context_contributions")
        elif corruption == "different_registry":
            connection.execute("UPDATE context_contributions SET contribution_id='unrelated-existing-source'")
        elif corruption == "changed_revision":
            connection.execute("UPDATE context_contributions SET source_revision='another-source-revision'")
        connection.commit()
        original_fingerprint = fingerprint(connection)
        # snapshot/hash/旧派生索引仍一致；拒绝必须来自独立的 source registry 证据。
        from app.services.infrastructure.rollout_context.migration.schema_v3.manifest import (
            Schema3AssemblyManifestValidator,
        )
        from app.services.infrastructure.rollout_context.migration.schema_v4.model import (
            parse_snapshot,
        )

        snapshot = parse_snapshot(connection.execute("SELECT snapshot_json FROM context_assemblies").fetchone()[0])
        Schema3AssemblyManifestValidator()._validate_context_assembly_manifest(connection, snapshot)
    before = immutable_files(source.root)
    with pytest.raises(RuntimeError, match="source-mismatch: selection.*schema3"):
        source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        assert fingerprint(connection) == original_fingerprint
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (3, "active")
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE from_version=3").fetchone() == (0,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='context_plans'").fetchone() is None
    assert immutable_files(source.root) == before


def test_import_source_manifest_never_opens_body_files(source, monkeypatch):
    body_root = source.root / "context-plan-details"
    assert body_root.is_dir() and any(body_root.rglob("*"))
    before = immutable_files(source.root)
    original_open = builtins.open
    original_path_open = Path.open

    def check_path(path):
        if not isinstance(path, int) and Path(path).is_relative_to(body_root):
            raise AssertionError("source manifest import 不得打开任何 detail 正文文件")

    def file_open(path, *args, **kwargs):
        check_path(path)
        return original_open(path, *args, **kwargs)

    def path_open(path, *args, **kwargs):
        check_path(path)
        return original_path_open(path, *args, **kwargs)

    def forbidden_body(*args, **kwargs):
        raise AssertionError("source manifest import 不得调用正文恢复能力")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", file_open)
        patch.setattr(Path, "open", path_open)
        patch.setattr(source.saver._detail_store, "read", forbidden_body)
        patch.setattr(source.saver, "_recover_request_source", forbidden_body)
        source.saver.upgrade_rollout_schema(source.session_id)
        registration = source.saver.get_context_plan_registration(source.session_id, plan_id=source.plan_id)
        assert registration.source_manifest is not None
        assert all(item["body"] is None for item in registration.source_manifest["contributions"])
    assert immutable_files(source.root) == before


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
def test_process_exit_is_atomic_and_explicit_retry_is_idempotent(source, stage):
    files = immutable_files(source.root)
    result = subprocess.run([sys.executable, "-c", """
import os
import sys
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.migration.schema_v4 import PreparedSchemaV4Upgrade
from app.services.infrastructure.rollout_context.storage.migrations import RolloutSchemaMigrationMixin
if sys.argv[3] == 'before_commit':
    original = PreparedSchemaV4Upgrade.verify_migrated
    def crash(self, connection):
        original(self, connection)
        assert connection.in_transaction
        os._exit(94)
    PreparedSchemaV4Upgrade.verify_migrated = crash
else:
    original = RolloutSchemaMigrationMixin._migrate_schema_locked
    def crash(self, *args, **kwargs):
        original(self, *args, **kwargs)
        os._exit(94)
    RolloutSchemaMigrationMixin._migrate_schema_locked = crash
with RolloutCheckpointSaver(sys.argv[1]) as saver:
    saver.upgrade_rollout_schema(sys.argv[2])
""", str(source.saver._storage.sessions_dir), source.session_id, stage], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 94, (result.stdout, result.stderr)
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (3 if stage == "before_commit" else 4, "active")
        assert connection.execute("SELECT count(*) FROM schema_migrations WHERE status='started'").fetchone() == (0,)
    source.saver.upgrade_rollout_schema(source.session_id)
    source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT count(*) FROM schema_migrations WHERE from_version=3 AND to_version=4 AND status='completed'").fetchone() == (1,)
    source.saver.get_context_assembly(source.session_id, assembly_id=source.assembly_id).validate_hashes()
    assert immutable_files(source.root) == files


def test_frozen_schema3_validator_does_not_require_schema4_registry(source, monkeypatch):
    from app.services.infrastructure.rollout_context.assembly.manifest import (
        ContextAssemblyManifestMixin,
    )
    from app.services.infrastructure.rollout_context.migration.schema_v3.sql import (
        validate_target,
    )

    def forbidden_runtime(*args, **kwargs):
        raise AssertionError("schema2→3 中间态不能调用 schema4 runtime validator")

    monkeypatch.setattr(ContextAssemblyManifestMixin, "_validate_context_assembly_manifest", forbidden_runtime)
    with closing(sqlite3.connect(source.index)) as connection:
        before = fingerprint(connection)
        validate_target(connection, checkpoint_ns="")
        assert fingerprint(connection) == before


def test_public_schema2_to_3_to_4_uses_frozen_intermediate_validator(source, monkeypatch):
    from app.services.infrastructure.rollout_context.migration.schema_v3.journal import (
        PreparedSchemaV3Upgrade,
    )
    from tests.integration.backend.sessions.rollout_context.schema_v4_legacy_bridge import (
        freeze_schema2_details,
    )

    freeze_schema2_details(source)
    original_files = immutable_files(source.root)
    validate = PreparedSchemaV3Upgrade.verify_migrated
    observed = []

    def verify(self, connection):
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (3,)
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='context_plans'").fetchone() is None
        validate(self, connection)
        observed.append(3)

    monkeypatch.setattr(PreparedSchemaV3Upgrade, "verify_migrated", verify)
    source.saver.upgrade_rollout_schema(source.session_id)
    source.saver.upgrade_rollout_schema(source.session_id)
    assert observed and set(observed) == {3}
    after = immutable_files(source.root)
    assert {path: after[path] for path in original_files} == original_files
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT from_version,to_version FROM schema_migrations WHERE from_version>0 ORDER BY migration_id").fetchall() == [(2, 3), (3, 4)]
        assert connection.execute("SELECT registration_origin FROM context_plans").fetchone() == ("schema3_import",)
    snapshot = source.saver.get_context_assembly(source.session_id, assembly_id=source.assembly_id)
    detail = next(entry.detail_ref for entry in snapshot.selection if entry.detail_ref is not None)
    assert source.saver.read_context_plan_detail(source.session_id, detail_ref=detail)["detail"] == source.body
