"""通过真实 v2 schema1 索引验证显式升级、备份和 SQL 原子事务。"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.itemized.hashing import contribution_content_hash
from app.domain.itemized.request_plan import ContextContribution
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.schema_upgrade import (
    execute_atomic_schema_sql,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.schema_legacy_fixtures import (
    freeze_empty_legacy_database,
)


@dataclass(frozen=True)
class OldSchema:
    sessions: Path
    session_id: str
    root: Path

    @property
    def index(self) -> Path:
        return self.root / "index.sqlite"

    @property
    def jsonl(self) -> Path:
        return self.root / "rollout.jsonl"


@pytest.fixture
def old_schema(
    request: pytest.FixtureRequest,
    session_bundle_factory: Callable[[Path, str], Path],
) -> OldSchema:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = context.workspace_root / ".boxteam" / "sessions"
    session_id = f"schema-{uuid4().hex}"
    node = session_bundle_factory(sessions, session_id)
    fixture = OldSchema(sessions, session_id, node / "rollout")
    with RolloutCheckpointSaver(sessions) as saver:
        saver.accept_turn(
            session_id, accepted_ingress_id="schema-ingress",
            acceptance_idempotency_key="schema-acceptance", payload="升级必须保留真实 root",
        )
        saver.register_context_contribution(session_id, ContextContribution(
            contribution_id="old-policy", source_kind="workspace_policy", source_revision="v1",
            body="已有策略", content_hash=contribution_content_hash("prompt", "已有策略"),
        ))
    freeze_empty_legacy_database(fixture.index, version=1)
    return fixture


def test_read_requires_explicit_upgrade_without_modifying_old_index(old_schema: OldSchema) -> None:
    before = old_schema.jsonl.read_bytes()
    storage = RolloutCheckpointSaver(old_schema.sessions)._storage
    with pytest.raises(RuntimeError, match="schema-upgrade-required"):
        storage.initialize(old_schema.session_id)
    with pytest.raises(RuntimeError, match="schema-upgrade-required"):
        storage.open_read_snapshot(old_schema.session_id)
    assert old_schema.jsonl.read_bytes() == before
    with sqlite3.connect(old_schema.index) as connection:
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (1,)
    assert not list(old_schema.root.glob("index.sqlite.migration-*.backup"))


def test_explicit_upgrade_preserves_items_sources_and_allows_digest_only(old_schema: OldSchema) -> None:
    before = old_schema.jsonl.read_bytes()
    with sqlite3.connect(old_schema.index) as connection:
        items = connection.execute("SELECT * FROM item_catalog").fetchall()
        contributions = connection.execute("SELECT * FROM context_contributions").fetchall()
    saver = RolloutCheckpointSaver(old_schema.sessions)
    saver.upgrade_rollout_schema(old_schema.session_id)
    assert old_schema.jsonl.read_bytes() == before
    with sqlite3.connect(old_schema.index) as connection:
        assert connection.execute("SELECT * FROM item_catalog").fetchall() == items
        assert connection.execute("SELECT * FROM context_contributions").fetchall() == contributions
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (4,)
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE from_version = 1 AND to_version = 2"
        ).fetchone() == ("completed",)
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE from_version = 2 AND to_version = 3"
        ).fetchone() == ("completed",)
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE from_version = 3 AND to_version = 4"
        ).fetchone() == ("completed",)
        assert connection.execute("SELECT COUNT(*) FROM context_plans").fetchone() == (0,)
        detail_columns = {row[1] for row in connection.execute("PRAGMA table_info(context_plan_details)")}
        assert {"detail_id", "detail_kind", "retention_class", "visibility", "expires_at"} <= detail_columns
        assert "gc_after" not in detail_columns
    saver.register_context_contribution(old_schema.session_id, ContextContribution(
        contribution_id="protected-policy", source_kind="workspace_policy", source_revision="v2",
        content_length=12, protection="protected", visibility="private",
        redacted_stable_digest="hmac-sha256:session:v1:" + "1" * 64,
    ))
    saver.upgrade_rollout_schema(old_schema.session_id)
    assert len(list(old_schema.root.glob("index.sqlite.migration-*.backup"))) == 3


def test_invalid_old_hash_pair_aborts_upgrade_and_preserves_originals(old_schema: OldSchema) -> None:
    with sqlite3.connect(old_schema.index) as connection:
        connection.execute("UPDATE context_contributions SET redacted_stable_digest = 'invalid-pair'")
        original = connection.execute("SELECT * FROM context_contributions").fetchall()
    before = old_schema.jsonl.read_bytes()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        RolloutCheckpointSaver(old_schema.sessions).upgrade_rollout_schema(old_schema.session_id)
    assert old_schema.jsonl.read_bytes() == before
    with sqlite3.connect(old_schema.index) as connection:
        assert connection.execute("SELECT * FROM context_contributions").fetchall() == original
        assert connection.execute("SELECT schema_version, database_state FROM database_meta").fetchone() == (1, "recovery_required")
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'schema_upgrade_%'"
        ).fetchall()
        assert connection.execute("SELECT status FROM schema_migrations ORDER BY migration_id DESC LIMIT 1").fetchone() == ("failed",)


def test_artifact_validation_failure_rolls_back_schema_before_commit(old_schema: OldSchema) -> None:
    storage = RolloutCheckpointSaver(old_schema.sessions)._storage
    before = old_schema.jsonl.read_bytes()
    with sqlite3.connect(old_schema.index) as connection:
        items = connection.execute("SELECT * FROM item_catalog").fetchall()
    validated = []

    def reject_migrated_artifact(connection: sqlite3.Connection) -> None:
        assert connection.in_transaction
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (2,)
        assert connection.execute("SELECT value FROM artifact_probe").fetchone() == ("published",)
        validated.append(True)
        raise RuntimeError("source-mismatch: published artifact verification failed")

    with (
        pytest.raises(RuntimeError, match="published artifact verification failed"),
        storage._lock(old_schema.session_id, ""),
    ):
        storage._migrate_schema_locked(
            old_schema.session_id, checkpoint_ns="", to_version=2,
            migration_name="artifact_validation_probe",
            migration_sql="CREATE TABLE artifact_probe(value TEXT); INSERT INTO artifact_probe VALUES ('published');",
            validate_migrated=reject_migrated_artifact,
        )
    assert validated == [True]
    assert old_schema.jsonl.read_bytes() == before
    with sqlite3.connect(old_schema.index) as connection:
        assert connection.execute("SELECT schema_version, database_state FROM database_meta").fetchone() == (1, "recovery_required")
        assert connection.execute("SELECT * FROM item_catalog").fetchall() == items
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name = 'artifact_probe'").fetchall()
        assert connection.execute("SELECT status FROM schema_migrations ORDER BY migration_id DESC LIMIT 1").fetchone() == ("failed",)


def test_process_exit_rolls_back_schema_ddl_before_explicit_retry(old_schema: OldSchema) -> None:
    before = old_schema.jsonl.read_bytes()
    with sqlite3.connect(old_schema.index) as connection:
        items = connection.execute("SELECT * FROM item_catalog").fetchall()
        contributions = connection.execute("SELECT * FROM context_contributions").fetchall()
    # 只替换故障注入点，DDL、SQLite 事务、文件锁和重启均经过真实 owner。
    process = subprocess.run(
        [sys.executable, "-c", """
import os
import sys
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.storage import migrations

execute = migrations.execute_atomic_schema_sql
def crash_after_ddl(connection, script):
    execute(connection, script)
    os._exit(91)

migrations.execute_atomic_schema_sql = crash_after_ddl
RolloutCheckpointSaver(sys.argv[1]).upgrade_rollout_schema(sys.argv[2])
""", str(old_schema.sessions), old_schema.session_id],
        check=False, capture_output=True, text=True, timeout=30,
    )
    assert process.returncode == 91, (process.stdout, process.stderr)
    assert old_schema.jsonl.read_bytes() == before
    with sqlite3.connect(old_schema.index) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT schema_version, database_state FROM database_meta").fetchone() == (1, "active")
        assert connection.execute("SELECT * FROM item_catalog").fetchall() == items
        assert connection.execute("SELECT * FROM context_contributions").fetchall() == contributions
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (1,)
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'schema_upgrade_%'").fetchall()
    backups = list(old_schema.root.glob("index.sqlite.migration-*.backup"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT * FROM item_catalog").fetchall() == items
    storage = RolloutCheckpointSaver(old_schema.sessions)._storage
    with pytest.raises(RuntimeError, match="schema-upgrade-required"):
        storage.initialize(old_schema.session_id)
    RolloutCheckpointSaver(old_schema.sessions).upgrade_rollout_schema(old_schema.session_id)
    with sqlite3.connect(old_schema.index) as connection:
        assert connection.execute("SELECT from_version,to_version FROM schema_migrations WHERE from_version>0 ORDER BY migration_id").fetchall() == [(1, 2), (2, 3), (3, 4)]
    assert old_schema.jsonl.read_bytes() == before


@pytest.fixture
def migration_connection() -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("BEGIN IMMEDIATE")
        yield connection


def test_atomic_script_handles_literal_and_trigger_semicolons(migration_connection: sqlite3.Connection) -> None:
    execute_atomic_schema_sql(migration_connection, """
        CREATE TABLE probe(value TEXT);
        CREATE TABLE audit(value TEXT);
        CREATE TRIGGER save_probe AFTER INSERT ON probe BEGIN
            INSERT INTO audit VALUES (new.value); INSERT INTO audit VALUES ('trigger;value');
        END;
        INSERT INTO probe VALUES ('literal;value');
    """)
    assert migration_connection.in_transaction
    assert migration_connection.execute("SELECT value FROM audit").fetchall() == [("literal;value",), ("trigger;value",)]
    migration_connection.rollback()
    assert not migration_connection.execute("SELECT name FROM sqlite_master WHERE name = 'probe'").fetchall()


@pytest.mark.parametrize("escape", ["COMMIT", "ROLLBACK", "SAVEPOINT migration_escape", "ATTACH ':memory:' AS other", "PRAGMA user_version=3"])
def test_migration_script_cannot_escape_transaction(migration_connection: sqlite3.Connection, escape: str) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        execute_atomic_schema_sql(migration_connection, f"CREATE TABLE probe(value); {escape};")
    assert migration_connection.in_transaction
    migration_connection.rollback()
    assert not migration_connection.execute("SELECT name FROM sqlite_master WHERE name = 'probe'").fetchall()
