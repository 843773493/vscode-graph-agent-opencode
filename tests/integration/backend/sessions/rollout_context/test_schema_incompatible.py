"""旧实验 v2 SQLite 只被明确拒绝；权威源文件与 frozen DDL 保持原样。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.core.identifier import create_uuid_hex
from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext

FROZEN_SCHEMA3 = Path("tests/integration/backend/sessions/rollout_context/schema_v4_fixtures/schema3.sql")


@pytest.fixture
def incompatible_rollout(request: pytest.FixtureRequest, session_bundle_factory):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session = "ses_" + create_uuid_hex()
    session_bundle_factory(sessions, session)
    with RolloutCheckpointSaver(sessions) as saver:
        storage = saver._storage
        thread = storage.storage.main_thread_id(session)
        root = storage.storage.root(session, thread_id=thread)
        shape = request.param
        if shape == "frozen-schema3":
            root.mkdir(parents=True, exist_ok=True)
            # 旧 schema 仍来自已物化的 rollout，读取应越过既有锁后拒绝 schema。
            (root.parent / ".rollout.write.lock").touch()
            with closing(sqlite3.connect(root / "index.sqlite")) as connection, connection:
                connection.executescript((Path.cwd() / FROZEN_SCHEMA3).read_text())
                connection.execute(
                    "INSERT INTO database_meta(singleton_id,rollout_id,session_id,schema_version,"
                    "message_format_version,database_state,last_message_sequence,last_control_sequence,"
                    "committed_jsonl_offset,projection_epoch,created_at,updated_at) "
                    "VALUES(1,?,?,3,1,'active',0,0,0,1,?,?)",
                    ("rollout-old-experiment", session, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
                )
                body = "旧实验 policy 正文必须保留"
                connection.execute(
                    "INSERT INTO context_contributions(contribution_id,source_kind,source_revision,"
                    "content_hash,request_only,metadata_json,created_at) VALUES(?,?,?,?,1,?,?)",
                    ("old-policy", "environment", "old-revision", contribution_content_hash("prompt", body),
                     canonical_json_bytes({"opaque": {"source_ref": "旧扩展保持"}}).decode(),
                     "2026-09-01T00:00:00Z"),
                )
            (root / "rollout.jsonl").write_bytes(
                canonical_json_bytes({"format_version": 2, "metadata": {"opaque": "旧实验原件"}}) + b"\n"
            )
        else:
            accepted = saver.accept_turn(
                session, thread_id=thread, accepted_ingress_id="schema-ingress",
                acceptance_idempotency_key="schema-accept", job_id="schema-job",
                admission_idempotency_key="schema-admission", payload="当前真实 canonical 输入保留",
            )
            assert accepted["turn_id"]
            with closing(sqlite3.connect(root / "index.sqlite")) as connection, connection:
                if shape == "old-version":
                    connection.execute("UPDATE database_meta SET schema_version=3")
                elif shape == "old-schema13":
                    connection.execute("UPDATE database_meta SET schema_version=13")
                elif shape == "missing-tool-result-index":
                    connection.execute("DROP INDEX item_catalog_background_result_identity")
                elif shape == "contribution-metadata":
                    connection.execute(
                        "ALTER TABLE context_contributions RENAME COLUMN binding_manifest_json TO metadata_json"
                    )
                else:
                    assert shape == "item-metadata"
                    connection.execute(
                        "ALTER TABLE item_catalog RENAME COLUMN projection_manifest_json TO metadata_json"
                    )
        yield saver, session, thread, root, shape


def _source_files(root: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
        )
        for path in root.rglob("*")
        if path.is_file() and path.name != "index.sqlite-shm"
        and (path.name != "index.sqlite-wal" or path.stat().st_size > 0)
    }


@pytest.mark.parametrize(
    "incompatible_rollout",
    ["frozen-schema3", "old-version", "old-schema13", "missing-tool-result-index", "contribution-metadata", "item-metadata"],
    indirect=True,
)
@pytest.mark.parametrize("operation", ["initialize", "open_read_snapshot", "read_items", "committed_tool_result_id"])
def test_old_experimental_shape_is_rejected_without_source_rewrite(
    incompatible_rollout, operation: str
) -> None:
    saver, session, thread, root, shape = incompatible_rollout
    before = _source_files(root)
    with pytest.raises(RuntimeError, match="schema-incompatible") as caught:
        if operation == "committed_tool_result_id":
            saver.committed_tool_result_id(session, thread, "model", "call")
        elif operation == "read_items":
            saver._storage.storage.read_items(session, thread)
        else:
            getattr(saver._storage.storage, operation)(session, thread_id=thread)
    if "metadata" in shape or shape == "frozen-schema3":
        assert "metadata" in str(caught.value)
    elif shape == "missing-tool-result-index":
        assert "ToolResult identity 索引" in str(caught.value)
    else:
        assert f"current={13 if shape == 'old-schema13' else 3}" in str(caught.value)
    assert _source_files(root) == before
    assert not tuple(root.glob("index.sqlite.migration-*.backup"))
    assert not tuple(root.glob("schema-upgrade-*"))
    with closing(sqlite3.connect(root / "index.sqlite")) as connection:
        assert not connection.execute(
            "SELECT 1 FROM schema_migrations WHERE from_version != 0"
        ).fetchall()
