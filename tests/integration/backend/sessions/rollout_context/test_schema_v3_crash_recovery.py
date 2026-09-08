"""真实子进程退出覆盖暂存/排他发布/SQL提交前的 schema3 升级重试。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.migration.schema_v3 import (
    PreparedSchemaV3Upgrade,
    prepare_schema_v3_upgrade,
    resume_schema_v3_upgrade_audits,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_schema_v3_artifact_upgrade import (
    schema2_artifact as schema2_artifact,  # noqa: PLC0414 - 显式导出 pytest fixture
)


@pytest.mark.parametrize("phase", ["partial_file", "after_link", "before_commit"])
def test_process_exit_before_commit_preserves_original_and_retries(schema2_artifact, phase, request):
    source = schema2_artifact
    before = (source.root / "rollout.jsonl").read_bytes()
    process = subprocess.run([sys.executable, "-c", """
import os
import sys
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.migration.schema_v3 import journal

phase = sys.argv[3]
write = journal.write_private
link = journal.os.link
validate = journal.PreparedSchemaV3Upgrade.verify_migrated

def interrupted_write(path, raw):
    if phase == 'partial_file' and path.name.startswith('.detail-schema3-') and 'staged' not in path.parts:
        write(path, raw[:9])
        os._exit(91)
    return write(path, raw)

def interrupted_link(source, target, **kwargs):
    link(source, target, **kwargs)
    if phase == 'after_link' and target.name.startswith('detail-schema3-') and 'staged' not in target.parts:
        os._exit(91)

def interrupted_commit(self, connection):
    validate(self, connection)
    if phase == 'before_commit':
        os._exit(91)

journal.write_private = interrupted_write
journal.os.link = interrupted_link
journal.PreparedSchemaV3Upgrade.verify_migrated = interrupted_commit
RolloutCheckpointSaver(sys.argv[1]).upgrade_rollout_schema(sys.argv[2])
""", str(source.saver._storage.sessions_dir), source.session_id, phase], capture_output=True, text=True, check=False)
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"{phase}.stdout.log").write_text(process.stdout)
    (context.artifacts_dir / f"{phase}.stderr.log").write_text(process.stderr)
    assert process.returncode == 91, process.stderr
    assert (source.root / "rollout.jsonl").read_bytes() == before
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (2,)
    with RolloutCheckpointSaver(source.saver._storage.sessions_dir) as restarted:
        restarted.upgrade_rollout_schema(source.session_id)
        restarted.get_context_assembly(source.session_id, assembly_id=source.assembly_id).validate_hashes()
    assert (source.root / "rollout.jsonl").read_bytes() == before


def test_resume_without_matching_journal_cannot_release_migrating(schema2_artifact):
    source = schema2_artifact
    with closing(sqlite3.connect(source.index)) as connection:
        prepared = prepare_schema_v3_upgrade(connection, rollout_root=source.root,
            session_id=source.session_id, checkpoint_ns="", detail_capability=None)
        connection.execute("UPDATE database_meta SET schema_version=3,database_state='migrating'")
        connection.commit()
        assert resume_schema_v3_upgrade_audits(connection, rollout_root=source.root, checkpoint_ns="") is False
        assert not (prepared.audit_root / "completed.json").exists()
        assert connection.execute("SELECT database_state FROM database_meta").fetchone() == ("migrating",)


@pytest.mark.parametrize("corruption", ["sql", "source_backup", "old_file", "new_file", "completed_file", "path_escape", "file_and_manifest", "target_fingerprint"])
def test_resume_rejects_corruption_without_activating(schema2_artifact, corruption, monkeypatch):
    source = schema2_artifact
    committed = PreparedSchemaV3Upgrade.verify_committed

    def stop_after_audit(self, connection):
        committed(self, connection)
        raise RuntimeError("injected-schema3-audit-before-activation")

    # 在真实 2→3 COMMIT/audit 完成后停止，不先升级到 4 再假改版号；
    # 否则这些用例只会命中版本门，完全没有校验各自的损坏内容。
    with monkeypatch.context() as patch:
        patch.setattr(PreparedSchemaV3Upgrade, "verify_committed", stop_after_audit)
        with pytest.raises(RuntimeError, match="injected-schema3-audit-before-activation"):
            source.saver.upgrade_rollout_schema(source.session_id)
    audit, = (source.root / "schema-upgrade-v3").iterdir()
    value = json.loads((audit / "prepared.json").read_bytes())
    if corruption == "sql":
        (audit / "migration.sql").write_bytes(b"SELECT 1;")
    elif corruption == "source_backup":
        (audit / "index.schema2.backup").write_bytes(b"not sqlite")
    elif corruption == "old_file":
        old = next(path for path in value["original_files"] if path != "rollout.jsonl")
        (source.root / old).write_bytes(b"changed old detail")
    elif corruption == "new_file":
        (source.root / next(iter(value["new_files"]))).write_bytes(b"changed new detail")
    elif corruption == "completed_file":
        (audit / "completed.json").write_bytes(b"{}")
    elif corruption == "file_and_manifest":
        relative = next(iter(value["new_files"]))
        raw = b"changed target and unauthenticated manifest"
        (source.root / relative).write_bytes(raw)
        value["new_files"][relative] = hashlib.sha256(raw).hexdigest()
        (audit / "prepared.json").write_text(json.dumps(value))
    elif corruption == "target_fingerprint":
        value["target_fingerprint"] = "sha256:jcs:v1:" + "a" * 64
        (audit / "prepared.json").write_text(json.dumps(value))
    else:
        value["new_files"] = {"../outside": "a" * 64}
        (audit / "prepared.json").write_text(json.dumps(value))
    with closing(sqlite3.connect(source.index)) as connection:
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (3, "migrating")
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='context_plans'").fetchone() is None
        with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
            resume_schema_v3_upgrade_audits(connection, rollout_root=source.root, checkpoint_ns="")
        assert connection.execute("SELECT database_state FROM database_meta").fetchone() == ("migrating",)


def test_resume_rejects_connection_owned_by_another_rollout(schema2_artifact):
    source = schema2_artifact
    source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(":memory:")) as foreign, pytest.raises(RuntimeError, match="connection 不属于"):
        resume_schema_v3_upgrade_audits(foreign, rollout_root=source.root, checkpoint_ns="")
