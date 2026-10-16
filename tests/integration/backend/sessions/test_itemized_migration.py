"""真实 v1/SQLite 到 v2 staging 的迁移集成验收。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.domain.itemized.errors import FormatDispatchError
from app.services.infrastructure.rollout_context.checkpoint.projection.message_projections import (
    RolloutMessageProjectionMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.migration import (
    importer as migration_importer,
)
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
)
from app.services.infrastructure.rollout_context.migration.importer import (
    LegacyRolloutImporter,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.itemized_migration_helpers import (
    _accepted_records,
    _storage,
    _table_counts,
    _write_v1_source,
    migration_audits,
    prepare_migration_workspace,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    copy_rollout_snapshot,
)

SOURCE_SESSION_ID = "ses_019c205391bb7a4b81b6728df70f63e6"
TARGET_SESSION_ID = "ses_019b9187b87f78c78ef242bbd6043bfc"
FORK_SESSION_ID = "ses_019c332b2c2a7210860822f55c60fa08"


@pytest.fixture
def migration_workspace(request: pytest.FixtureRequest) -> Path:
    return prepare_migration_workspace(request)


@pytest.mark.parametrize("invocation", ["direct", "module"])
def test_v1_cli_uses_catalog_owner_and_round_trips_nonempty_namespace(
    migration_workspace: Path,
    session_bundle_factory,
    request: pytest.FixtureRequest,
    invocation: str,
) -> None:
    sessions = migration_workspace / ".boxteam" / "sessions"
    _write_v1_source(
        sessions,
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions, TARGET_SESSION_ID)
    storage = _storage(sessions)
    namespace = "explicit-import-namespace"
    source_before = artifact_manifest(storage.storage.root(SOURCE_SESSION_ID))
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    entry = (
        ["scripts/legacy_import_v1_to_v2.py"]
        if invocation == "direct"
        else ["-m", "scripts.legacy_import_v1_to_v2"]
    )
    common = [
        "--sessions-dir",
        str(sessions),
        "--source-session-id",
        SOURCE_SESSION_ID,
        "--checkpoint-ns",
        namespace,
    ]
    for mode in ("report", "import"):
        process = subprocess.run(
            [
                "timeout",
                "30",
                "bash",
                "-c",
                'ulimit -d 4194304; exec "$@"',
                "bash",
                "uv",
                "run",
                "python",
                *entry,
                *common,
                *(
                    ["--report-only"]
                    if mode == "report"
                    else ["--target-session-id", TARGET_SESSION_ID]
                ),
            ],
            capture_output=True,
            text=True,
            timeout=35,
            check=False,
        )
        (context.artifacts_dir / f"cli-{invocation}-{mode}.log").write_text(
            process.stdout + process.stderr
        )
        assert process.returncode == 0, process.stderr
        result = json.loads(process.stdout)
        assert result["source_thread_id"] == storage.storage.main_thread_id(SOURCE_SESSION_ID)
        assert result["checkpoint_ns"] == namespace
        assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == source_before
        if mode == "report":
            assert result["read_only"] is True
            assert not storage.storage.root(TARGET_SESSION_ID).exists()
            assert migration_audits(storage) == []
        else:
            assert result["target_thread_id"] == storage.storage.main_thread_id(
                TARGET_SESSION_ID
            )
            assert result["status"] == "completed"
    script = """
import json
import sys
from tests.support.session_catalog_owner import fixture_catalog_scope
from tests.integration.backend.sessions.itemized_migration_helpers import _storage
with fixture_catalog_scope():
    storage = _storage(sys.argv[1]).storage
    session, namespace = sys.argv[2:4]
    thread = storage.main_thread_id(session)
    items = storage.read_items(session, thread, checkpoint_ns=namespace)
    with storage._connect(thread, namespace, session_id=session, read_only=True) as connection:
        owner = connection.execute("SELECT session_id, thread_id FROM database_meta").fetchone()
        binding = connection.execute("SELECT COUNT(*) FROM item_catalog i JOIN executions e ON e.execution_id=i.execution_id AND e.session_id=? AND e.thread_id=?", (session, thread)).fetchone()[0]
    assert not hasattr(storage, "import_v1")
    print(json.dumps({"owner": owner, "payloads": [item.payload for item in items], "execution_bound_items": binding}))
"""
    restarted = subprocess.run(
        [
            "timeout",
            "30",
            "bash",
            "-c",
            'ulimit -d 4194304; exec "$@"',
            "bash",
            "uv",
            "run",
            "python",
            "-c",
            script,
            str(sessions),
            TARGET_SESSION_ID,
            namespace,
        ],
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    (context.artifacts_dir / f"cli-{invocation}-reopen.log").write_text(
        restarted.stdout + restarted.stderr
    )
    assert restarted.returncode == 0, restarted.stderr
    assert json.loads(restarted.stdout) == {
        "owner": [TARGET_SESSION_ID, storage.storage.main_thread_id(TARGET_SESSION_ID)],
        "payloads": ["迁移前的请求", "迁移后的回答"],
        "execution_bound_items": 2,
    }


@pytest.mark.parametrize(
    "owner_field", ["source_thread_id", "target_thread_id", "checkpoint_ns"]
)
def test_v1_importer_rejects_wrong_owner_before_staging(
    migration_workspace: Path,
    session_bundle_factory,
    owner_field: str,
) -> None:
    sessions = migration_workspace / ".boxteam" / "sessions"
    _write_v1_source(
        sessions,
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions, TARGET_SESSION_ID)
    storage = _storage(sessions)
    source_before = artifact_manifest(storage.storage.root(SOURCE_SESSION_ID))
    options = {
        "source_session_id": SOURCE_SESSION_ID,
        "source_thread_id": storage.storage.main_thread_id(SOURCE_SESSION_ID),
        "target_session_id": TARGET_SESSION_ID,
        "target_thread_id": storage.storage.main_thread_id(TARGET_SESSION_ID),
        "checkpoint_ns": "explicit-import-namespace",
    }
    if owner_field == "checkpoint_ns":
        options[owner_field] = None
    else:
        options[owner_field] = options[
            "target_thread_id"
            if owner_field == "source_thread_id"
            else "source_thread_id"
        ]
    expected_error = RuntimeError if owner_field == "checkpoint_ns" else ValueError
    with pytest.raises(expected_error, match="main thread|checkpoint_ns"):
        LegacyRolloutImporter(storage).import_v1(**options)
    assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == source_before
    assert not storage.storage.root(TARGET_SESSION_ID).exists()
    assert migration_audits(storage) == []


def test_v1_report_is_read_only_and_migration_installs_target_v2(
    migration_workspace: Path,
    session_bundle_factory,
) -> None:
    source_records = _accepted_records()
    source_lines = _write_v1_source(
        migration_workspace / ".boxteam" / "sessions",
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=source_records,
    )
    session_bundle_factory(
        migration_workspace / ".boxteam" / "sessions", TARGET_SESSION_ID
    )
    storage = _storage(migration_workspace / ".boxteam" / "sessions")

    report = LegacyRolloutImporter(storage).read_report(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        checkpoint_ns="",
    )
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
    source_rollout = storage.storage.root(SOURCE_SESSION_ID)
    assert (source_rollout / "rollout.jsonl").read_bytes() == source_lines

    result = LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
    )

    assert result["status"] == "completed"
    assert len(result["migrated"]) == 1
    assert result["rejected"] == []
    assert _table_counts(storage, TARGET_SESSION_ID) == {
        "item_catalog": 2,
        "messages": 2,
        "turn_records": 1,
        "executions": 1,
        "legacy_migration_reports": 1,
    }
    migrated_items = storage.storage.read_items(
        TARGET_SESSION_ID, storage.storage.main_thread_id(TARGET_SESSION_ID)
    )
    assert [item.payload for item in migrated_items] == ["迁移前的请求", "迁移后的回答"]
    assert all(item.item_id.startswith("item-legacy:") for item in migrated_items)
    with storage.storage._connect(
        storage.storage.main_thread_id(TARGET_SESSION_ID),
        "",
        read_only=True,
        session_id=TARGET_SESSION_ID,
    ) as connection:
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
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    storage = _storage(sessions_root)

    with pytest.raises(ValueError, match="source/target.*同一个 session"):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=SOURCE_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            checkpoint_ns="",
        )


@pytest.mark.asyncio
async def test_full_copy_v1_source_requires_explicit_import_without_side_effects(
    migration_workspace: Path,
    session_bundle_factory,
) -> None:
    sessions_root = migration_workspace / ".boxteam" / "sessions"
    source_lines = _write_v1_source(
        sessions_root,
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions_root, TARGET_SESSION_ID)
    storage = _storage(sessions_root)
    source_index = storage.storage.index_path(SOURCE_SESSION_ID).read_bytes()

    with (
        pytest.raises(FormatDispatchError, match="v1_migration_required"),
        RolloutCheckpointSaver(sessions_root, storage=storage) as saver,
    ):
        await copy_rollout_snapshot(saver, SOURCE_SESSION_ID, TARGET_SESSION_ID)
    assert not storage.storage.root(TARGET_SESSION_ID).exists()
    assert migration_audits(storage) == []
    assert (
        storage.storage.root(SOURCE_SESSION_ID).joinpath("rollout.jsonl").read_bytes()
        == source_lines
    )
    assert storage.storage.index_path(SOURCE_SESSION_ID).read_bytes() == source_index


@pytest.mark.asyncio
async def test_explicit_v1_import_then_full_copy_records_v2_source_lineage(
    migration_workspace: Path,
    session_bundle_factory,
) -> None:
    sessions_root = migration_workspace / ".boxteam" / "sessions"
    _write_v1_source(
        sessions_root,
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions_root, TARGET_SESSION_ID)
    session_bundle_factory(sessions_root, FORK_SESSION_ID)
    storage = _storage(sessions_root)

    result = LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
        require_lossless=True,
    )
    assert result["status"] == "completed"
    source_items = storage.storage.read_items(
        TARGET_SESSION_ID, storage.storage.main_thread_id(TARGET_SESSION_ID)
    )
    with RolloutCheckpointSaver(sessions_root, storage=storage) as saver:
        copied = await copy_rollout_snapshot(saver, TARGET_SESSION_ID, FORK_SESSION_ID)
    fork_items = storage.storage.read_items(FORK_SESSION_ID, storage.storage.main_thread_id(FORK_SESSION_ID))
    assert [item.to_dict() for item in fork_items] == [item.to_dict() for item in source_items]
    assert all(not item.item_id.startswith("legacy:v1:") for item in fork_items)
    with storage.storage.read_connection(FORK_SESSION_ID, storage.storage.main_thread_id(FORK_SESSION_ID), "") as connection:
        assert connection.execute("SELECT source_session_id,source_view_id,fork_mode FROM fork_origins WHERE fork_id=?", (copied.fork_id,)).fetchone() == (
            TARGET_SESSION_ID, copied.source_view_id, "full_rollout_copy",
        )


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
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions, TARGET_SESSION_ID)
    storage = _storage(sessions)
    index = storage.storage.index_path(SOURCE_SESSION_ID).read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        assert not storage.storage.root(TARGET_SESSION_ID).exists()
        raise RuntimeError("injected migration failure")

    owner, method = {
        "projection": (
            RolloutMessageProjectionMixin,
            "_materialize_canonical_message_projection",
        ),
        "validation": (migration_importer, "validate_staging"),
        "installation": (migration_importer, "install_directory"),
    }[failure_point]
    monkeypatch.setattr(owner, method, fail)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    assert not storage.storage.root(TARGET_SESSION_ID).exists()
    audit = migration_audits(storage)[0]
    assert (audit["status"], audit["rollback"]) == (
        "failed",
        "uninstalled_staging_quarantined",
    )
    assert storage.storage.jsonl_path(SOURCE_SESSION_ID).read_bytes() == lines
    assert storage.storage.index_path(SOURCE_SESSION_ID).read_bytes() == index


def test_migration_target_remains_absent_until_validated_directory_install(
    migration_workspace: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = migration_workspace / ".boxteam" / "sessions"
    _write_v1_source(
        sessions,
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions, TARGET_SESSION_ID)
    storage = _storage(sessions)
    install = migration_importer.install_directory

    def inspect(staging: Path, target: Path) -> None:
        assert not target.exists()
        with sqlite3.connect(staging / "index.sqlite") as connection:
            assert connection.execute("SELECT status FROM turn_records").fetchone() == (
                "completed",
            )
        assert (staging / "rollout.jsonl").read_bytes()
        install(staging, target)

    monkeypatch.setattr(migration_importer, "install_directory", inspect)
    LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
    )
    assert (
        len(
            storage.storage.read_items(
                TARGET_SESSION_ID, storage.storage.main_thread_id(TARGET_SESSION_ID)
            )
        )
        == 2
    )
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
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions, TARGET_SESSION_ID)
    storage = _storage(sessions)
    with sqlite3.connect(storage.storage.index_path(SOURCE_SESSION_ID)) as connection:
        connection.execute(corruption)
    before = (
        storage.storage.index_path(SOURCE_SESSION_ID).read_bytes(),
        storage.storage.jsonl_path(SOURCE_SESSION_ID).read_bytes(),
    )
    with pytest.raises((FormatDispatchError, RuntimeError), match=pattern):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    assert not storage.storage.root(TARGET_SESSION_ID).exists()
    assert migration_audits(storage)[0]["status"] == "failed"
    assert before == (
        storage.storage.index_path(SOURCE_SESSION_ID).read_bytes(),
        storage.storage.jsonl_path(SOURCE_SESSION_ID).read_bytes(),
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
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=records,
    )
    session_bundle_factory(sessions, TARGET_SESSION_ID)
    storage = _storage(sessions)
    with pytest.raises(FormatDispatchError, match="format_version"):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    assert not storage.storage.root(TARGET_SESSION_ID).exists()
    assert migration_audits(storage)[0]["status"] == "failed"


@pytest.mark.parametrize(
    "metadata,expected,expected_outcome,expected_evidence",
    [
        ({}, "unknown", "unknown", "legacy_finalization_unavailable"),
        ({"final": True}, "completed", "unknown", "explicit_legacy_final_marker"),
        ({"status": "failed"}, "failed", "failed", "explicit_legacy_control_marker"),
        ({"status": "interrupted"}, "interrupted", "interrupted", "explicit_legacy_control_marker"),
        ({"status": "cancelled"}, "cancelled", "cancelled", "explicit_legacy_control_marker"),
        ({"final": True, "status": "failed"}, "unknown", "unknown", "ambiguous_legacy_finalization"),
    ],
)
def test_finalization_requires_unambiguous_legacy_evidence(
    migration_workspace: Path,
    session_bundle_factory,
    metadata: dict[str, object],
    expected: str,
    expected_outcome: str,
    expected_evidence: str,
) -> None:
    sessions = migration_workspace / ".boxteam" / "sessions"
    records = _accepted_records()
    records[1]["metadata"] = metadata
    _write_v1_source(
        sessions,
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=records,
    )
    session_bundle_factory(sessions, TARGET_SESSION_ID)
    storage = _storage(sessions)
    result = LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
    )
    migrated = result["migrated"][0]
    assert migrated["status"] == expected
    assert migrated["finalization_evidence"] == expected_evidence
    with storage.storage._connect(
        storage.storage.main_thread_id(TARGET_SESSION_ID),
        read_only=True,
        session_id=TARGET_SESSION_ID,
    ) as connection:
        turn_id, turn, final, initial_execution_id = connection.execute(
            "SELECT turn_id, status, final_item_id, initial_execution_id FROM turn_records"
        ).fetchone()
        execution_id, execution, first_call, last_call, job_id = connection.execute(
            "SELECT execution_id, outcome, first_model_call_id, last_model_call_id, job_id FROM executions"
        ).fetchone()
        assert connection.execute("SELECT identity_origin FROM turn_acceptances").fetchall() == [
            ("legacy_synthetic",)
        ]
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone()[0] == 0
        commit_id, commit_outcome, commit_metadata = connection.execute(
            "SELECT commit_id, outcome, metadata_json FROM storage_commits "
            "WHERE commit_kind='terminal_convergence' AND subject_id=?",
            (turn_id,),
        ).fetchone()
        assert commit_outcome == expected_outcome
        assert json.loads(commit_metadata) == {
            "legacy_source_session_id": SOURCE_SESSION_ID,
            "legacy_source_thread_id": storage.storage.main_thread_id(SOURCE_SESSION_ID),
            "finalization_evidence": expected_evidence,
        }
        if final is not None:
            assert connection.execute(
                "SELECT turn_id, execution_id, commit_id, semantic_kind, status "
                "FROM item_catalog WHERE item_id=?", (final,),
            ).fetchone() == (
                turn_id, execution_id, commit_id, "assistant_output", "completed",
            )
    assert turn == expected
    assert execution == expected_outcome
    assert initial_execution_id == execution_id == migrated["target_initial_execution_id"]
    assert first_call is last_call is None
    assert (final is not None) is (expected == "completed")
    target_thread_id = storage.storage.main_thread_id(TARGET_SESSION_ID)
    assert storage.executions.cold_execution_observations(TARGET_SESSION_ID, target_thread_id) == []
    assert storage.executions.cold_execution_observations(
        TARGET_SESSION_ID, target_thread_id, turn_id=turn_id,
    ) == []
    assert storage.executions.cold_execution_observations(
        TARGET_SESSION_ID, target_thread_id, job_ids=[job_id],
    ) == []
    if expected == "interrupted":
        resumed = storage.executions.resume_turn(
            TARGET_SESSION_ID, thread_id=target_thread_id, turn_id=turn_id,
            job_id="live-import-resume-job",
        )
        assert resumed["attempt"] == 2
        fresh = _storage(sessions)
        resumed_rows = fresh.executions.cold_execution_observations(
            TARGET_SESSION_ID, target_thread_id, turn_id=turn_id,
        )
        assert len(resumed_rows) == 1
        assert (resumed_rows[0]["job_id"], resumed_rows[0]["execution_id"], resumed_rows[0]["state"]) == (
            resumed["job_id"], resumed["execution_id"], "pending",
        )
        assert fresh.executions.cold_execution_observations(
            TARGET_SESSION_ID, target_thread_id, job_ids=[job_id],
        ) == []
        fresh.executions.converge_execution(
            TARGET_SESSION_ID, thread_id=target_thread_id, turn_id=turn_id,
            execution_id=resumed["execution_id"], outcome="interrupted", turn_status="interrupted",
        )
        latest = fresh.executions.resume_turn(
            TARGET_SESSION_ID, thread_id=target_thread_id, turn_id=turn_id,
            job_id="live-import-second-resume-job",
        )
        assert latest["attempt"] == 3
        fresh = _storage(sessions)
        latest_rows = fresh.executions.cold_execution_observations(
            TARGET_SESSION_ID, target_thread_id, turn_id=turn_id,
        )
        assert len(latest_rows) == 1
        assert (latest_rows[0]["job_id"], latest_rows[0]["execution_id"], latest_rows[0]["state"]) == (
            latest["job_id"], latest["execution_id"], "pending",
        )
        job_rows = fresh.executions.cold_execution_observations(
            TARGET_SESSION_ID, target_thread_id, job_ids=[resumed["job_id"], latest["job_id"], job_id],
        )
        assert [(row["job_id"], row["execution_id"], row["state"]) for row in job_rows] == [
            (latest["job_id"], latest["execution_id"], "pending"),
            (resumed["job_id"], resumed["execution_id"], "cancelled"),
        ]
        assert fresh.executions.cold_execution_observations(TARGET_SESSION_ID, target_thread_id) == job_rows
        return
    accepted = storage.executions.accept_turn(
        TARGET_SESSION_ID, thread_id=target_thread_id,
        accepted_ingress_id="live-import-successor-ingress",
        acceptance_idempotency_key="live-import-successor-acceptance",
        job_id="live-import-successor-job",
        admission_idempotency_key="live-import-successor-admission",
        payload="导入后新接受的真实请求",
    )
    pending = storage.executions.cold_execution_observations(TARGET_SESSION_ID, target_thread_id)
    assert len(pending) == 1
    assert (pending[0]["job_id"], pending[0]["execution_id"], pending[0]["state"]) == (
        "live-import-successor-job", accepted["initial_execution_id"], "pending",
    )
