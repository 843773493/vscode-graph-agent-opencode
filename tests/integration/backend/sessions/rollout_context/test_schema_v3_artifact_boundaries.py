"""schema2 显式升级的 namespace、metadata 和非覆盖路径边界。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.migration.schema_v3 import (
    prepare_schema_v3_upgrade,
)
from tests.integration.backend.sessions.rollout_context.schema_v3_helpers import (
    artifact_manifest,
    assert_original_artifacts_unchanged,
)
from tests.integration.backend.sessions.rollout_context.test_schema_v3_artifact_upgrade import (
    schema2_artifact as schema2_artifact,  # noqa: PLC0414 - 显式导出 pytest fixture
)


def test_upgrade_preserves_all_namespaces_in_shared_index(schema2_artifact):
    source = schema2_artifact
    with closing(sqlite3.connect(source.index)) as connection, connection:
        connection.execute("UPDATE context_plan_details SET checkpoint_ns='nested/checkpoint'")
        row = connection.execute("SELECT * FROM context_plan_details LIMIT 1").fetchone()
        other = list(row)
        other[0], other[3] = "unbound-detail", "unbound-assembly"
        other[2] = "another/checkpoint"
        other[4] = "rollout/context-plan-details/unbound-assembly/unbound-detail.json"
        old = json.loads((source.root.parent / row[4]).read_bytes())
        old["assembly_id"] = other[3]
        path = source.root.parent / other[4]
        path.parent.mkdir()
        path.write_bytes(canonical_json_bytes(old))
        other[5] = sha256_jcs(old)
        connection.execute("INSERT INTO context_plan_details VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", other)
    source.saver.upgrade_rollout_schema(source.session_id)
    with closing(sqlite3.connect(source.index)) as connection:
        assert {row[0] for row in connection.execute("SELECT checkpoint_ns FROM context_plan_details")} == {"nested/checkpoint", "another/checkpoint"}
        for key, namespace in connection.execute("SELECT detail_ref,checkpoint_ns FROM context_plan_details"):
            ref = detail_ref_from_key(key)
            assert source.saver.read_context_plan_detail(source.session_id, detail_ref=ref, checkpoint_ns=namespace)["checkpoint_ns"] == namespace


def test_metadata_operational_refs_remap_but_lineage_stays_original(schema2_artifact):
    source = schema2_artifact
    with closing(sqlite3.connect(source.index)) as connection, connection:
        old_id = connection.execute("SELECT detail_ref FROM context_plan_details LIMIT 1").fetchone()[0]
        metadata = {"nested": [{"source_ref": old_id}], "legacy_source_ref": {"detail_ref": old_id}}
        for table in ("context_contributions", "context_assembly_contributions"):
            connection.execute(f"UPDATE {table} SET metadata_json=?", (canonical_json_bytes(metadata).decode(),))
        value = json.loads(connection.execute("SELECT snapshot_json FROM context_assemblies").fetchone()[0])
        for contribution in value["contributions"]:
            contribution["metadata"] = metadata
        connection.execute("UPDATE context_assemblies SET snapshot_json=?", (canonical_json_bytes(value).decode(),))
    source.saver.upgrade_rollout_schema(source.session_id)
    snapshot = source.saver.get_context_assembly(source.session_id, assembly_id=source.assembly_id)
    remapped = snapshot.to_dict()["contributions"][0]["metadata"]
    assert remapped["legacy_source_ref"] == {"detail_ref": old_id}
    assert remapped["nested"][0]["source_ref"]["session_id"] == source.session_id
    assert remapped["nested"][0]["source_ref"]["detail_id"] != old_id
    with closing(sqlite3.connect(source.index)) as connection:
        for table in ("context_contributions", "context_assembly_contributions"):
            assert json.loads(connection.execute(f"SELECT metadata_json FROM {table}").fetchone()[0]) == remapped


@pytest.mark.parametrize("link", ["symlink", "hardlink", "missing_symlink"])
def test_unsafe_source_detail_is_rejected_before_audit(schema2_artifact, link):
    source = schema2_artifact
    with closing(sqlite3.connect(source.index)) as connection, connection:
        relative = connection.execute("SELECT relative_path FROM context_plan_details LIMIT 1").fetchone()[0]
        if link == "missing_symlink":
            connection.execute("UPDATE context_plan_details SET status='unavailable',availability='unavailable'")
    path = source.root.parent / relative
    original = path.with_name(path.name + ".preserved")
    path.rename(original)
    if link == "hardlink":
        os.link(original, path)
    else:
        path.symlink_to(original if link == "symlink" else original.with_name("absent-source"))
    before = original.read_bytes()
    with pytest.raises(RuntimeError, match="符号链接|硬链接"):
        source.saver.upgrade_rollout_schema(source.session_id)
    assert original.read_bytes() == before
    assert not (source.root / "schema-upgrade-v3").exists()


def test_source_mutation_between_prepare_and_publish_never_publishes(schema2_artifact):
    source = schema2_artifact
    with closing(sqlite3.connect(source.index)) as connection:
        prepared = prepare_schema_v3_upgrade(connection, rollout_root=source.root,
            session_id=source.session_id, checkpoint_ns="", detail_capability=None)
        relative = next(path for path in prepared.original_files if path != "rollout.jsonl")
        (source.root / relative).write_bytes(b"external corruption")
        before = artifact_manifest(source.root)
        with pytest.raises(RuntimeError, match="迁移原件变化"):
            prepared.publish()
        assert_original_artifacts_unchanged(source.root, before)
        assert not any((source.root / path).exists() for path in prepared.new_files)


def test_checkpoint_table_mutation_between_prepare_and_publish_is_not_ignored(schema2_artifact):
    source = schema2_artifact
    with closing(sqlite3.connect(source.index)) as connection:
        prepared = prepare_schema_v3_upgrade(connection, rollout_root=source.root,
            session_id=source.session_id, checkpoint_ns="", detail_capability=None)
        connection.execute("UPDATE branches SET status='changed-after-prepare'")
        connection.commit()
        with pytest.raises(RuntimeError, match="SQLite 发生变化"):
            prepared.publish()
        assert not any((source.root / path).exists() for path in prepared.new_files)


def test_prepare_and_source_snapshot_do_not_release_an_existing_sqlite_lock(schema2_artifact):
    source = schema2_artifact
    # DELETE 模式下 RESERVED 锁可由另一进程精确观测；测试结束不改变生产配置。
    with closing(sqlite3.connect(source.index)) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN IMMEDIATE")
        with closing(sqlite3.connect(source.index.as_uri() + "?mode=ro", uri=True)) as reader:
            prepare_schema_v3_upgrade(reader, rollout_root=source.root,
                session_id=source.session_id, checkpoint_ns="", detail_capability=None)
            artifact_manifest(source.root)
            probe = subprocess.run([sys.executable, "-c", """
import sqlite3
import sys
with sqlite3.connect(sys.argv[1], timeout=0) as connection:
    try:
        connection.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError as error:
        assert 'locked' in str(error), str(error)
    else:
        raise AssertionError('migration/source snapshot 关闭额外 fd 释放了父进程 SQLite 锁')
""", str(source.index)], check=False, capture_output=True, text=True, timeout=30)
            assert probe.returncode == 0, (probe.stdout, probe.stderr)
        writer.rollback()
