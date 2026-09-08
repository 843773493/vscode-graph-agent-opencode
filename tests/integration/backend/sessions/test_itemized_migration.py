"""真实 v1/SQLite 到 v2 staging 的迁移集成验收。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.domain.itemized.errors import FormatDispatchError
from app.services.infrastructure.rollout_context.migration import (
    store as migration_store,
)
from app.services.infrastructure.rollout_context.migration.projections import (
    LegacyMigrationProjectionMixin,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
)
from tests.integration.backend.sessions.itemized_migration_helpers import (
    _accepted_records,
    _storage,
    _table_counts,
    _write_v1_source,
    migration_audits,
    prepare_migration_workspace,
)


@pytest.fixture
def migration_workspace(request: pytest.FixtureRequest) -> Path:
    return prepare_migration_workspace(request)


def test_v1_report_is_read_only_and_migration_installs_target_v2(
    migration_workspace: Path,
    session_bundle_factory,
) -> None:
    source_records = _accepted_records()
    source_lines = _write_v1_source(
        migration_workspace / ".boxteam" / "sessions",
        session_id="source",
        session_bundle_factory=session_bundle_factory,
        records=source_records,
    )
    session_bundle_factory(migration_workspace / ".boxteam" / "sessions", "target")
    storage = _storage(migration_workspace / ".boxteam" / "sessions")

    report = storage.legacy_migration_report("source")
    assert report["read_only"] is True
    assert report["source_format_version"] == 1
    assert report["dispatch_contract"] == {
        "source_reader": "legacy_import_v1_to_v2_only",
        "target_runtime_format_version": 2,
        "v1_runtime_fallback": False,
        "source_coordinates_are_audit_only": True,
    }
    source_artifacts = report["source_artifacts"]
    assert source_artifacts["rollout_jsonl"]["size"] == len(source_lines)
    assert source_artifacts["rollout_jsonl"]["committed_offset"] == len(source_lines)
    assert len(source_artifacts["rollout_jsonl"]["sha256"]) == 64
    assert len(source_artifacts["index_sqlite"]["sha256"]) == 64
    assert [candidate["candidate_status"] for candidate in report["candidates"]] == [
        "accepted"
    ]
    source_rollout = storage.root("source")
    assert (source_rollout / "rollout.jsonl").read_bytes() == source_lines

    result = storage.migrate_legacy_to_v2("source", target_thread_id="target")

    assert result["status"] == "completed"
    assert len(result["migrated"]) == 1
    assert result["rejected"] == []
    assert _table_counts(storage, "target") == {
        "item_catalog": 2,
        "messages": 2,
        "turn_records": 1,
        "executions": 1,
        "legacy_migration_reports": 1,
    }
    migrated_items = storage.read_items("target")
    assert [item.payload for item in migrated_items] == ["迁移前的请求", "迁移后的回答"]
    assert all(item.item_id.startswith("item-legacy:") for item in migrated_items)
    with storage._connect("target", "", read_only=True) as connection:
        format_version, state = connection.execute(
            "SELECT rollout_format_version, database_state FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        migration_status = connection.execute(
            "SELECT status FROM legacy_migration_reports"
        ).fetchone()[0]
    assert (format_version, state, migration_status) == (2, "active", "completed")
    assert (source_rollout / "rollout.jsonl").read_bytes() == source_lines


def test_legacy_migration_rejects_same_source_and_target_session(
    migration_workspace: Path,
    session_bundle_factory,
) -> None:
    sessions_root = migration_workspace / ".boxteam" / "sessions"
    _write_v1_source(
        sessions_root,
        session_id="source",
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    storage = _storage(sessions_root)

    with pytest.raises(ValueError, match="source/target.*同一个 session"):
        storage.migrate_legacy_to_v2("source", target_thread_id="source")


def test_full_copy_v1_source_runs_explicit_staging_and_keeps_source_artifacts(
    migration_workspace: Path,
    session_bundle_factory,
) -> None:
    sessions_root = migration_workspace / ".boxteam" / "sessions"
    source_lines = _write_v1_source(
        sessions_root,
        session_id="source",
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions_root, "target")
    storage = _storage(sessions_root)
    source_index = storage.index_path("source").read_bytes()

    assert (
        storage.clone_rollout(
            source_thread_id="source",
            target_thread_id="target",
            source_checkpoint_id=None,
        )
        is None
    )

    with storage._connect("target", "", read_only=True) as connection:
        format_version, state, item_count, migration_status = connection.execute(
            "SELECT database_meta.rollout_format_version, database_meta.database_state, "
            "(SELECT COUNT(*) FROM item_catalog), "
            "(SELECT status FROM legacy_migration_reports ORDER BY created_at DESC LIMIT 1) "
            "FROM database_meta"
        ).fetchone()
    assert (format_version, state, item_count, migration_status) == (
        2,
        "active",
        2,
        "completed",
    )
    assert storage.root("source").joinpath("rollout.jsonl").read_bytes() == source_lines
    assert storage.index_path("source").read_bytes() == source_index


def test_full_copy_v1_commit_records_legacy_source_lineage(
    migration_workspace: Path,
    session_bundle_factory,
) -> None:
    sessions_root = migration_workspace / ".boxteam" / "sessions"
    _write_v1_source(
        sessions_root,
        session_id="source",
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions_root, "target")
    storage = _storage(sessions_root)

    assert (
        storage.clone_rollout(
            source_thread_id="source",
            target_thread_id="target",
            source_checkpoint_id=None,
        )
        is None
    )
    materialization_id, fork_id = storage.begin_fork_materialization(
        target_session_id="target",
        source_session_id="source",
        source_checkpoint_id=None,
        source_view_id=None,
        fork_mode="full_rollout_copy",
        relationship="detached",
    )
    assert (
        storage.commit_fork_materialization(
            materialization_id,
            target_session_id="target",
            source_session_id="source",
            source_checkpoint_id=None,
            source_view_id=None,
            fork_mode="full_rollout_copy",
            relationship="detached",
        )
        == fork_id
    )

    with storage._connect("target", "", read_only=True) as connection:
        mappings = connection.execute(
            "SELECT entity_type, source_local_id, target_local_id, source_offset, "
            "target_offset, lineage_json "
            "FROM fork_identity_mappings WHERE fork_id = ? ORDER BY entity_type, source_local_id",
            (fork_id,),
        ).fetchall()
        origin = connection.execute(
            "SELECT source_view_id, fork_mode FROM fork_origins WHERE fork_id = ?",
            (fork_id,),
        ).fetchone()
    by_type = {str(row[0]): row for row in mappings}
    assert by_type["item"][1].startswith("legacy:v1:message:")
    assert by_type["turn"][1] == "legacy:v1:turn:legacy-turn-1"
    assert by_type["execution"][1].startswith("legacy:v1:execution:")
    assert by_type["item"][2] != by_type["item"][1]
    item_offsets = sorted(
        (int(row[3]), int(row[4])) for row in mappings if row[0] == "item"
    )
    first_source_length = len(canonical_json_line(_accepted_records()[0]))
    assert [offset[0] for offset in item_offsets] == [0, first_source_length]
    assert item_offsets[0][1] == 0
    assert item_offsets[1][1] > item_offsets[0][1]
    assert json.loads(str(by_type["item"][5]))["identity_mode"] == (
        "legacy_migrated_target_local"
    )
    assert origin == (None, "full_rollout_copy")


@pytest.mark.parametrize("failure_point", ["projection", "validation", "installation"])
def test_migration_failure_quarantines_staging_without_creating_target(
    migration_workspace: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    sessions = migration_workspace / ".boxteam" / "sessions"
    lines = _write_v1_source(
        sessions,
        session_id="source",
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions, "target")
    storage = _storage(sessions)
    index = storage.index_path("source").read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        assert not storage.root("target").exists()
        raise RuntimeError("injected migration failure")

    owner, method = {
        "projection": (
            LegacyMigrationProjectionMixin,
            "_install_migration_message_projections",
        ),
        "validation": (migration_store, "_validate_staging"),
        "installation": (migration_store, "install_directory"),
    }[failure_point]
    monkeypatch.setattr(owner, method, fail)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        storage.migrate_legacy_to_v2("source", target_thread_id="target")
    assert not storage.root("target").exists()
    audit = migration_audits(storage)[0]
    assert (audit["status"], audit["rollback"]) == (
        "failed",
        "uninstalled_staging_quarantined",
    )
    assert storage.jsonl_path("source").read_bytes() == lines
    assert storage.index_path("source").read_bytes() == index


def test_migration_target_remains_absent_until_validated_directory_install(
    migration_workspace: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = migration_workspace / ".boxteam" / "sessions"
    _write_v1_source(
        sessions,
        session_id="source",
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions, "target")
    storage = _storage(sessions)
    install = migration_store.install_directory

    def inspect(staging: Path, target: Path) -> None:
        assert not target.exists()
        with sqlite3.connect(staging / "index.sqlite") as connection:
            assert connection.execute("SELECT status FROM turn_records").fetchone() == (
                "completed",
            )
        assert (staging / "rollout.jsonl").read_bytes()
        install(staging, target)

    monkeypatch.setattr(migration_store, "install_directory", inspect)
    storage.migrate_legacy_to_v2("source", target_thread_id="target")
    assert len(storage.read_items("target")) == 2
    assert migration_audits(storage)[0]["status"] == "installed"


@pytest.mark.parametrize(
    "corruption,pattern",
    [
        (
            "UPDATE messages SET message_id='mismatch' WHERE message_sequence=1",
            "manifest 与 JSONL",
        ),
        (
            "UPDATE messages SET jsonl_length='not-an-integer' WHERE message_sequence=1",
            "jsonl_length",
        ),
        (
            "UPDATE database_meta SET committed_jsonl_offset='not-an-integer'",
            "committed_jsonl_offset",
        ),
        ("UPDATE database_meta SET rollout_format_version=9", "rollout_format_version"),
        ("ALTER TABLE database_meta DROP COLUMN rollout_format_version", "缺少字段"),
    ],
)
def test_corrupt_source_is_rejected_and_only_failure_audit_is_written(
    migration_workspace: Path,
    session_bundle_factory,
    corruption: str,
    pattern: str,
) -> None:
    sessions = migration_workspace / ".boxteam" / "sessions"
    _write_v1_source(
        sessions,
        session_id="source",
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions, "target")
    storage = _storage(sessions)
    with sqlite3.connect(storage.index_path("source")) as connection:
        connection.execute(corruption)
    before = (
        storage.index_path("source").read_bytes(),
        storage.jsonl_path("source").read_bytes(),
    )
    with pytest.raises((FormatDispatchError, RuntimeError), match=pattern):
        storage.migrate_legacy_to_v2("source", target_thread_id="target")
    assert not storage.root("target").exists()
    assert migration_audits(storage)[0]["status"] == "failed"
    assert before == (
        storage.index_path("source").read_bytes(),
        storage.jsonl_path("source").read_bytes(),
    )


@pytest.mark.parametrize("version", [9, 2, True, "1", 1.0])
def test_unknown_or_mixed_envelope_format_is_not_coerced(
    migration_workspace: Path,
    session_bundle_factory,
    version: object,
) -> None:
    sessions = migration_workspace / ".boxteam" / "sessions"
    records = _accepted_records()
    records[1]["format_version"] = version
    _write_v1_source(
        sessions,
        session_id="source",
        session_bundle_factory=session_bundle_factory,
        records=records,
    )
    session_bundle_factory(sessions, "target")
    storage = _storage(sessions)
    with pytest.raises(FormatDispatchError, match="format_version"):
        storage.migrate_legacy_to_v2("source", target_thread_id="target")
    assert not storage.root("target").exists()
    assert migration_audits(storage)[0]["status"] == "failed"


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({}, "unknown"),
        ({"final": True}, "completed"),
        ({"status": "failed"}, "failed"),
        ({"status": "interrupted"}, "interrupted"),
        ({"status": "cancelled"}, "cancelled"),
        ({"final": True, "status": "failed"}, "unknown"),
    ],
)
def test_finalization_requires_unambiguous_legacy_evidence(
    migration_workspace: Path,
    session_bundle_factory,
    metadata: dict[str, object],
    expected: str,
) -> None:
    sessions = migration_workspace / ".boxteam" / "sessions"
    records = _accepted_records()
    records[1]["metadata"] = metadata
    _write_v1_source(
        sessions,
        session_id="source",
        session_bundle_factory=session_bundle_factory,
        records=records,
    )
    session_bundle_factory(sessions, "target")
    storage = _storage(sessions)
    result = storage.migrate_legacy_to_v2("source", target_thread_id="target")
    assert result["migrated"][0]["status"] == expected
    with storage._connect("target", read_only=True) as connection:
        turn, final = connection.execute(
            "SELECT status, final_item_id FROM turn_records"
        ).fetchone()
        execution = connection.execute("SELECT outcome FROM executions").fetchone()[0]
    assert (turn, execution) == (expected, expected)
    assert (final is not None) is (expected == "completed")
