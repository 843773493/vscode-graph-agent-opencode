"""schema2 非空 artifact 显式升级：真实 Saver、SQLite 事务与 immutable 文件。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.migration.schema_v3 import (
    PreparedSchemaV3Upgrade,
    prepare_schema_v3_upgrade,
)
from tests.integration.backend.sessions.itemized_migration_helpers import (
    prepare_migration_workspace,
)
from tests.integration.backend.sessions.rollout_context.schema_v3_helpers import (
    Schema2Artifact,
    artifact_manifest,
    assert_original_artifacts_unchanged,
    create_schema2_artifact,
)


@pytest.fixture
def schema2_artifact(request, session_bundle_factory):
    sessions = prepare_migration_workspace(request) / ".boxteam" / "sessions"
    session_id = "schema2-" + uuid4().hex
    session_bundle_factory(sessions, session_id)
    with RolloutCheckpointSaver(sessions) as saver:
        artifact = create_schema2_artifact(saver, session_id)
        yield artifact


def _prepare(source, connection):
    return prepare_schema_v3_upgrade(connection, rollout_root=source.root,
        session_id=source.session_id, checkpoint_ns="", detail_capability=None)


def test_public_assembly_upgrade_restores_body_with_unchanged_canonical(schema2_artifact: Schema2Artifact):
    source = schema2_artifact
    original = artifact_manifest(source.root)
    original_jsonl = (source.root / "rollout.jsonl").read_bytes()
    with closing(sqlite3.connect(source.index)) as connection:
        item_rows = connection.execute("SELECT * FROM item_catalog").fetchall()
    source.saver.upgrade_rollout_schema(source.session_id)
    audit, = (source.root / "schema-upgrade-v3").iterdir()
    assert (audit / "index.schema2.backup").is_file()
    assert (audit / "completed.json").is_file()
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT * FROM item_catalog").fetchall() == item_rows
    assert (source.root / "rollout.jsonl").read_bytes() == original_jsonl
    after = artifact_manifest(source.root)
    for path, identity in original.items():
        if path not in {"index.sqlite", "index.sqlite-wal", "index.sqlite-shm"}:
            assert after[path] == identity
    with RolloutCheckpointSaver(source.saver._storage.sessions_dir) as restarted:
        restarted.upgrade_rollout_schema(source.session_id)
        snapshot = restarted.get_context_assembly(source.session_id, assembly_id=source.assembly_id)
        snapshot.validate_hashes()
        for entry in snapshot.selection:
            if entry.detail_ref is not None:
                assert isinstance(entry.detail_ref, DetailRef)
                entry.detail_ref.require_owner(source.session_id, snapshot.assembly_id)
                assert "detail_ref" not in entry.ref.to_dict()
                assert restarted.read_context_plan_detail(source.session_id, detail_ref=entry.detail_ref)["detail"] == source.body
        projected, losses = restarted.project_context_plan_with_diagnostics(source.session_id, snapshot.as_sealed_plan())
        assert not losses
        assert source.body[0]["text"] in json.dumps([message.model_dump(mode="json") for message in projected], ensure_ascii=False)


@pytest.mark.parametrize("corruption", ["plan_hash", "request_hash", "sql_ref", "jsonl_offset", "detail_hash", "detail_path", "protected_no_key"])
def test_preflight_rejection_preserves_all_originals(schema2_artifact, corruption):
    source = schema2_artifact
    with closing(sqlite3.connect(source.index)) as connection, connection:
        if corruption in {"plan_hash", "request_hash"}:
            connection.execute(f"UPDATE context_assemblies SET {corruption}='bad'")
        elif corruption == "sql_ref":
            connection.execute("UPDATE context_assembly_selections SET ref_id='foreign-ref' WHERE plan_ordinal=0")
        elif corruption == "jsonl_offset":
            connection.execute("UPDATE database_meta SET committed_jsonl_offset=1")
        elif corruption == "detail_hash":
            connection.execute("UPDATE context_plan_details SET content_hash='bad'")
        elif corruption == "detail_path":
            connection.execute("UPDATE context_plan_details SET relative_path='../outside'")
        else:
            connection.execute("UPDATE context_plan_details SET protection='protected',sensitive=1")
    before = artifact_manifest(source.root)
    with pytest.raises((RuntimeError, ValueError)):
        source.saver.upgrade_rollout_schema(source.session_id)
    assert_original_artifacts_unchanged(source.root, before)
    assert not (source.root / "schema-upgrade-v3").exists()


def test_published_uncommitted_upgrade_is_explicitly_retryable(schema2_artifact, monkeypatch):
    source = schema2_artifact
    with closing(sqlite3.connect(source.index)) as connection:
        prepared = _prepare(source, connection)
        prepared.publish()
        prepared.discard_uncommitted(connection)
        retried = _prepare(source, connection)
        assert retried.audit_id == prepared.audit_id
        assert retried.migration_sql == prepared.migration_sql
        retried.publish()
    committed = PreparedSchemaV3Upgrade.verify_committed
    observed = []

    def verify_schema3_twice(self, connection):
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (3,)
        committed(self, connection)
        committed(retried, connection)
        with pytest.raises(RuntimeError, match="already-committed"):
            retried.discard_uncommitted(connection)
        observed.append(True)

    with monkeypatch.context() as patch:
        patch.setattr(PreparedSchemaV3Upgrade, "verify_committed", verify_schema3_twice)
        with RolloutCheckpointSaver(source.saver._storage.sessions_dir) as restarted:
            restarted.upgrade_rollout_schema(source.session_id)
    assert observed == [True]
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (4,)
        with pytest.raises(RuntimeError, match="already-committed"):
            retried.discard_uncommitted(connection)


def test_precommit_validation_failure_cannot_publish_sql_identity(schema2_artifact, monkeypatch):
    source = schema2_artifact
    validate = PreparedSchemaV3Upgrade.verify_migrated

    def reject(self, connection):
        validate(self, connection)
        assert connection.in_transaction
        assert not (self.audit_root / "completed.json").exists()
        raise RuntimeError("injected-precommit-failure")

    monkeypatch.setattr(PreparedSchemaV3Upgrade, "verify_migrated", reject)
    with pytest.raises(RuntimeError, match="injected-precommit-failure"):
        source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (2, "recovery_required")
    audit, = (source.root / "schema-upgrade-v3").iterdir()
    assert (audit / "aborted.json").is_file()
    assert not (audit / "completed.json").exists()
