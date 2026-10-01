"""经公共 Saver 入口升级冻结旧密文，验证原件保留与无密钥拒绝。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.migration.schema_v3 import (
    PreparedSchemaV3Upgrade,
    prepare_schema_v3_upgrade,
    resume_schema_v3_upgrade_audits,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.binding import (
    bind_sql,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.schema_v3_helpers import (
    artifact_manifest,
    assert_original_artifacts_unchanged,
)
from tests.integration.backend.sessions.rollout_context.schema_v3_protected_helpers import (
    create_protected_schema2_artifact,
)
from tests.support.workspaces import prepare_default_test_workspace

SESSION_ID = "ses_e6d2707870e54cab8c135193c0802532"


@pytest.fixture
def protected_artifact(request, session_bundle_factory):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root / "cases" / uuid4().hex / "workspace",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    sessions = workspace / ".boxteam" / "sessions"
    session_bundle_factory(sessions, SESSION_ID)
    with RolloutCheckpointSaver(sessions, protected_detail_key=b"p" * 32) as saver:
        source = create_protected_schema2_artifact(saver)
        yield source


def test_public_saver_upgrades_frozen_protected_body_without_plaintext_audit(protected_artifact):
    source = protected_artifact
    before = artifact_manifest(source.root)
    source.saver.upgrade_rollout_schema(source.session_id)
    source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        ref = detail_ref_from_key(connection.execute("SELECT detail_ref FROM context_plan_details").fetchone()[0])
    with RolloutCheckpointSaver(source.saver._storage.sessions_dir, protected_detail_key=b"p" * 32) as restarted:
        assert restarted.read_context_plan_detail(source.session_id, detail_ref=ref, include_sensitive=True)["detail"] == source.body
        with pytest.raises(PermissionError):
            restarted.read_context_plan_detail(source.session_id, detail_ref=ref)
    after = artifact_manifest(source.root)
    for relative, identity in before.items():
        if relative not in {"index.sqlite", "index.sqlite-wal", "index.sqlite-shm"}:
            assert after[relative] == identity
    audit, = (source.root / "schema-upgrade-v3").iterdir()
    metadata = json.loads((audit / "prepared.json").read_bytes())
    assert any("protected" in path for path in metadata["original_files"])
    assert any("protected" in path for path in metadata["new_files"])
    for path in audit.rglob("*"):
        if path.is_file():
            assert b"schema2-protected-fixture" not in path.read_bytes()


@pytest.mark.parametrize("key", [None, b"wrong-key".ljust(32, b"!")])
def test_missing_or_wrong_key_fails_before_backup_and_publish(protected_artifact, key):
    source = protected_artifact
    before = artifact_manifest(source.root)
    with RolloutCheckpointSaver(source.saver._storage.sessions_dir, protected_detail_key=key) as saver, pytest.raises(RuntimeError, match="schema-upgrade-protected"):
        saver.upgrade_rollout_schema(source.session_id)
    assert_original_artifacts_unchanged(source.root, before)
    assert not (source.root / "schema-upgrade-v3").exists()


def test_irreversible_old_redacted_marker_never_gets_invented_type(protected_artifact):
    source = protected_artifact
    path = source.root / "context-plan-details/old-assembly/detail-old.json"
    value = json.loads(path.read_bytes())
    value.update(protection="redacted", protected_body=False)
    path.write_bytes(canonical_json_bytes(value))
    with closing(sqlite3.connect(source.index)) as connection, connection:
        connection.execute("UPDATE context_plan_details SET protection='redacted',content_hash=?", (sha256_jcs(value),))
    before = artifact_manifest(source.root)
    with pytest.raises(RuntimeError, match="schema-upgrade-redacted-type-required"):
        source.saver.upgrade_rollout_schema(source.session_id)
    assert_original_artifacts_unchanged(source.root, before)


def test_protected_published_before_commit_reuses_authenticated_ciphertext(protected_artifact, monkeypatch):
    source = protected_artifact
    originals = artifact_manifest(source.root)
    with closing(sqlite3.connect(source.index)) as connection:
        prepared = prepare_schema_v3_upgrade(
            connection, rollout_root=source.root, session_id=source.session_id,
            checkpoint_ns="", detail_capability=source.saver._detail_store.schema_v3_detail_capability(),
        )
        prepared.publish()
        prepared.discard_uncommitted(connection)
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (2,)
    published = {path: (source.root / path).read_bytes() for path in prepared.new_files}
    capability_type = type(source.saver._detail_store.schema_v3_detail_capability())
    original_prepare = capability_type.prepare_legacy_detail
    regenerated = []

    def observe_random_encryption(self, **kwargs):
        result = original_prepare(self, **kwargs)
        regenerated.append(result.protected_bytes)
        return result

    monkeypatch.setattr(capability_type, "prepare_legacy_detail", observe_random_encryption)
    with RolloutCheckpointSaver(source.saver._storage.sessions_dir, protected_detail_key=b"p" * 32) as restarted:
        restarted.upgrade_rollout_schema(source.session_id)
        with closing(sqlite3.connect(source.index)) as connection:
            ref = detail_ref_from_key(connection.execute("SELECT detail_ref FROM context_plan_details").fetchone()[0])
            assert connection.execute("SELECT count(*) FROM schema_migrations WHERE from_version=2 AND to_version=3 AND status='completed'").fetchone() == (1,)
        assert restarted.read_context_plan_detail(source.session_id, detail_ref=ref, include_sensitive=True)["detail"] == source.body
    assert (prepared.audit_root / "completed.json").is_file()
    assert list((source.root / "schema-upgrade-v3").iterdir()) == [prepared.audit_root]
    for path, raw in published.items():
        assert (source.root / path).read_bytes() == raw
    cipher = next(raw for path, raw in published.items() if "protected" in path)
    assert regenerated and all(raw != cipher for raw in regenerated)
    for path, identity in originals.items():
        if not path.startswith("index.sqlite"):
            assert artifact_manifest(source.root)[path] == identity


@pytest.mark.parametrize("phase", ["after_publish", "before_commit", "staged_before_prepared", "partial_staged_cipher", "after_commit_before_audit", "after_audit_before_activate", "staged_cipher_link", "prepared_link", "completed_link"])
def test_protected_process_exit_then_public_retry_keeps_original_bytes(protected_artifact, phase, request):
    source = protected_artifact
    before = artifact_manifest(source.root)
    process = subprocess.run([sys.executable, "-c", """
import os
import sys
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.migration.schema_v3 import journal

phase = sys.argv[3]
publish = journal.PreparedSchemaV3Upgrade.publish
validate = journal.PreparedSchemaV3Upgrade.verify_migrated
committed = journal.PreparedSchemaV3Upgrade.verify_committed
immutable = journal.immutable_file
write = journal.write_private
link = journal.os.link

def interrupted_publish(self):
    publish(self)
    if phase == 'after_publish':
        os._exit(92)

def interrupted_validate(self, connection):
    validate(self, connection)
    if phase == 'before_commit':
        os._exit(92)

def interrupted_committed(self, connection):
    if phase == 'after_commit_before_audit':
        os._exit(92)
    committed(self, connection)
    if phase == 'after_audit_before_activate':
        os._exit(92)

def interrupted_immutable(path, raw, **kwargs):
    if phase == 'staged_before_prepared' and path.name == 'prepared.json':
        os._exit(92)
    return immutable(path, raw, **kwargs)

def interrupted_write(path, raw):
    if phase == 'partial_staged_cipher' and 'staged' in path.parts and 'context-plan-details-protected' in path.parts:
        write(path, raw[:9])
        os._exit(92)
    return write(path, raw)

def interrupted_link(source, target, **kwargs):
    link(source, target, **kwargs)
    if ((phase == 'staged_cipher_link' and 'staged' in target.parts and 'context-plan-details-protected' in target.parts)
        or (phase == 'prepared_link' and target.name == 'prepared.json')
        or (phase == 'completed_link' and target.name == 'completed.json')):
        os._exit(92)

journal.PreparedSchemaV3Upgrade.publish = interrupted_publish
journal.PreparedSchemaV3Upgrade.verify_migrated = interrupted_validate
journal.PreparedSchemaV3Upgrade.verify_committed = interrupted_committed
journal.immutable_file = interrupted_immutable
journal.write_private = interrupted_write
journal.os.link = interrupted_link
RolloutCheckpointSaver(sys.argv[1], protected_detail_key=b'p' * 32).upgrade_rollout_schema(sys.argv[2])
""", str(source.saver._storage.sessions_dir), source.session_id, phase], capture_output=True, text=True, check=False)
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"{phase}.stdout.log").write_text(process.stdout)
    (context.artifacts_dir / f"{phase}.stderr.log").write_text(process.stderr)
    assert process.returncode == 92, process.stderr
    with closing(sqlite3.connect(source.index)) as connection:
        version = 3 if phase in {"after_commit_before_audit", "after_audit_before_activate", "completed_link"} else 2
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (version,)
        if version == 3:
            assert connection.execute("SELECT database_state FROM database_meta").fetchone() == ("migrating",)
            ref = detail_ref_from_key(connection.execute("SELECT detail_ref FROM context_plan_details").fetchone()[0])
            with pytest.raises(RuntimeError, match="schema-upgrade-required"):
                source.saver.read_context_plan_detail(source.session_id, detail_ref=ref, include_sensitive=True)
    public_ciphertexts = {path: path.read_bytes() for path in (source.root / "context-plan-details-protected").rglob("detail-schema3-*.bin")}
    with RolloutCheckpointSaver(source.saver._storage.sessions_dir, protected_detail_key=b"p" * 32) as restarted:
        restarted.upgrade_rollout_schema(source.session_id)
        with closing(sqlite3.connect(source.index)) as connection:
            ref = detail_ref_from_key(connection.execute("SELECT detail_ref FROM context_plan_details").fetchone()[0])
        assert restarted.read_context_plan_detail(source.session_id, detail_ref=ref, include_sensitive=True)["detail"] == source.body
    after = artifact_manifest(source.root)
    for path, identity in before.items():
        if not path.startswith("index.sqlite"):
            assert after[path] == identity
    assert all(path.read_bytes() == raw for path, raw in public_ciphertexts.items())


@pytest.mark.parametrize("rewrite_manifest", [False, True])
def test_retry_refuses_tampered_staged_cipher_even_with_rehashed_manifest(protected_artifact, rewrite_manifest):
    source = protected_artifact
    with closing(sqlite3.connect(source.index)) as connection:
        prepared = prepare_schema_v3_upgrade(connection, rollout_root=source.root,
            session_id=source.session_id, checkpoint_ns="",
            detail_capability=source.saver._detail_store.schema_v3_detail_capability())
    relative = next(path for path in prepared.new_files if "protected" in path)
    path = prepared.audit_root / "staged" / relative
    original = path.read_bytes()
    corrupted = original[:-1] + bytes([original[-1] ^ 1])
    path.write_bytes(corrupted)
    if rewrite_manifest:
        metadata = json.loads((prepared.audit_root / "prepared.json").read_bytes())
        metadata["new_files"][relative] = hashlib.sha256(corrupted).hexdigest()
        base_sql = prepared.migration_sql.partition("\n")[2]
        sql = bind_sql(base_sql, **{field: metadata[field] for field in (
            "source_fingerprint", "target_fingerprint", "original_files", "new_files", "checkpoint_ns",
        )})
        metadata["migration_checksum"] = hashlib.sha256(sql.encode()).hexdigest()
        (prepared.audit_root / "migration.sql").write_text(sql)
        (prepared.audit_root / "prepared.json").write_bytes(canonical_json_bytes(metadata))
    before = artifact_manifest(source.root)
    with pytest.raises(RuntimeError, match="schema-upgrade-(audit-conflict|protected)"):
        source.saver.upgrade_rollout_schema(source.session_id)
    assert_original_artifacts_unchanged(source.root, before)
    assert not any((source.root / relative).exists() for relative in prepared.new_files)


def test_protected_public_retry_after_sql_rollback_keeps_published_cipher(protected_artifact, monkeypatch):
    source = protected_artifact
    original_validate = PreparedSchemaV3Upgrade.verify_migrated

    def reject(self, connection):
        original_validate(self, connection)
        assert connection.in_transaction
        raise RuntimeError("injected-protected-precommit-failure")

    with monkeypatch.context() as patch:
        patch.setattr(PreparedSchemaV3Upgrade, "verify_migrated", reject)
        with pytest.raises(RuntimeError, match="injected-protected-precommit-failure"):
            source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (2, "recovery_required")
    audit, = (source.root / "schema-upgrade-v3").iterdir()
    assert not (audit / "completed.json").exists()
    metadata = json.loads((audit / "prepared.json").read_bytes())
    published = {relative: (source.root / relative).read_bytes() for relative in metadata["new_files"]}
    source.saver.upgrade_rollout_schema(source.session_id)
    for relative, raw in published.items():
        assert (source.root / relative).read_bytes() == raw
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (4, "active")
        ref = detail_ref_from_key(connection.execute("SELECT detail_ref FROM context_plan_details").fetchone()[0])
    assert source.saver.read_context_plan_detail(source.session_id, detail_ref=ref, include_sensitive=True)["detail"] == source.body


def test_completed_schema3_audit_cannot_release_other_version_migrating(protected_artifact):
    source = protected_artifact
    source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        connection.execute("UPDATE database_meta SET schema_version=4,database_state='migrating'")
        connection.commit()
        with pytest.raises(RuntimeError, match="不能解除其它版本"):
            resume_schema_v3_upgrade_audits(connection, rollout_root=source.root, checkpoint_ns="")
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (4, "migrating")
