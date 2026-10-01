"""迁移跨文件原子性、进程退出、路径安全和 source 语义验收。"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from app.domain.itemized.errors import FormatDispatchError
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.migration import (
    store as migration_store,
)
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
)
from app.services.infrastructure.rollout_context.migration.store import (
    LegacyMigrationStorage,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.itemized_migration_helpers import (
    _accepted_records,
    _storage,
    _write_v1_source,
    migration_audits,
    prepare_migration_workspace,
)

SOURCE_SESSION_ID = "ses_e6d2707870e54cab8c135193c0802532"
TARGET_SESSION_ID = "ses_58a5607fd562454a932d851c95b73cc4"
OTHER_TARGET_SESSION_ID = "ses_5ce2590d35c74fd9a71e8d7526be328c"


@pytest.fixture
def migration_setup(request: pytest.FixtureRequest, session_bundle_factory) -> Callable:
    workspace = prepare_migration_workspace(request)
    sessions = workspace / ".boxteam" / "sessions"

    def setup(records: list[dict[str, object]] | None = None) -> LegacyMigrationStorage:
        _write_v1_source(
            sessions,
            session_id=SOURCE_SESSION_ID,
            session_bundle_factory=session_bundle_factory,
            records=records if records is not None else _accepted_records(),
        )
        session_bundle_factory(sessions, TARGET_SESSION_ID)
        return _storage(sessions)

    return setup


@pytest.mark.parametrize("existing", ["empty_v2", "partial", "empty_directory"])
def test_preexisting_target_is_preserved(
    migration_setup: Callable, existing: str
) -> None:
    storage = migration_setup()
    target = storage.root(TARGET_SESSION_ID)
    if existing == "empty_v2":
        storage.initialize(TARGET_SESSION_ID)
    else:
        target.mkdir()
        if existing == "partial":
            (target / "rollout.jsonl").write_bytes(b"preexisting uncommitted data")
    before = artifact_manifest(target)
    if existing == "empty_directory":
        storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
        assert len(storage.read_items(TARGET_SESSION_ID)) == 2
    else:
        with pytest.raises(RuntimeError, match="空 rollout"):
            storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
        assert artifact_manifest(target) == before


@pytest.mark.parametrize(
    "location",
    [
        "source_jsonl",
        "source_index",
        "source_root",
        "target_root",
        "audit_root",
        "target_lock",
    ],
)
def test_symlink_path_is_rejected_without_touching_external_files(
    migration_setup: Callable, location: str
) -> None:
    storage = migration_setup()
    external = storage.root(TARGET_SESSION_ID).parent / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_bytes(b"must not change")
    paths = {
        "source_jsonl": storage.jsonl_path(SOURCE_SESSION_ID),
        "source_index": storage.index_path(SOURCE_SESSION_ID),
        "source_root": storage.root(SOURCE_SESSION_ID),
        "target_root": storage.root(TARGET_SESSION_ID),
        "audit_root": storage.root(TARGET_SESSION_ID).parent / "legacy-import",
        "target_lock": storage.root(TARGET_SESSION_ID).parent / ".rollout.write.lock",
    }
    path = paths[location]
    if path.exists():
        path.rename(path.with_name(path.name + ".original"))
    path.symlink_to(external if location.endswith("root") else sentinel)
    before = artifact_manifest(external)
    with pytest.raises(RuntimeError, match="符号链接|symlink"):
        storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
    assert artifact_manifest(external) == before


def test_hardlink_source_is_rejected(migration_setup: Callable) -> None:
    storage = migration_setup()
    os.link(storage.jsonl_path(SOURCE_SESSION_ID), storage.root(SOURCE_SESSION_ID) / "linked.jsonl")
    with pytest.raises(RuntimeError, match="硬链接"):
        storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
    assert not storage.root(TARGET_SESSION_ID).exists()


@pytest.mark.parametrize("phase", ["during_build", "before_install", "after_install"])
def test_process_exit_preserves_originals_and_recovers_only_audit(
    migration_setup: Callable,
    request: pytest.FixtureRequest,
    phase: str,
) -> None:
    storage = migration_setup()
    before = artifact_manifest(storage.root(SOURCE_SESSION_ID))
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    script = """
import os
import sys
from unittest.mock import patch
from app.services.infrastructure.rollout_context.migration import store
from tests.integration.backend.sessions.itemized_migration_helpers import _storage
from app.services.infrastructure.rollout_context.checkpoint.projection.message_projections import RolloutMessageProjectionMixin
original = store.install_directory
def crash(staging, target):
    if sys.argv[2] == "after_install":
        original(staging, target)
    os._exit(73)
owner, method = (RolloutMessageProjectionMixin, "_materialize_canonical_message_projection") if sys.argv[2] == "during_build" else (store, "install_directory")
def exit_during_build(*args, **kwargs):
    os._exit(73)
with patch.object(owner, method, exit_during_build if sys.argv[2] == "during_build" else crash):
    _storage(sys.argv[1]).migrate_legacy_to_v2(sys.argv[3], target_thread_id=sys.argv[4])
"""
    process = subprocess.run(
        ["uv", "run", "python", "-c", script, str(storage.sessions_dir), phase, SOURCE_SESSION_ID, TARGET_SESSION_ID],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    (context.artifacts_dir / f"{phase}.log").write_text(process.stdout + process.stderr)
    assert process.returncode == 73, process.stderr
    assert artifact_manifest(storage.root(SOURCE_SESSION_ID)) == before
    assert migration_audits(storage)[0]["status"] == (
        "running" if phase == "during_build" else "ready"
    )
    installed = phase == "after_install"
    target_before = artifact_manifest(storage.root(TARGET_SESSION_ID)) if installed else None
    recovered = _storage(storage.sessions_dir).recover_legacy_imports(TARGET_SESSION_ID)
    assert recovered[0]["status"] == ("installed" if installed else "failed")
    if installed:
        assert artifact_manifest(storage.root(TARGET_SESSION_ID)) == target_before
        assert len(storage.read_items(TARGET_SESSION_ID)) == 2
    else:
        assert not storage.root(TARGET_SESSION_ID).exists()
        storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
        assert [report["status"] for report in migration_audits(storage)].count(
            "installed"
        ) == 1


def test_post_install_error_cannot_erase_published_history(
    migration_setup: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = migration_setup()
    install = migration_store.install_directory

    def fail_after(staging: Path, target: Path) -> None:
        install(staging, target)
        raise OSError("injected post-rename failure")

    monkeypatch.setattr(migration_store, "install_directory", fail_after)
    with pytest.raises(OSError, match="post-rename"):
        storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
    before = artifact_manifest(storage.root(TARGET_SESSION_ID))
    assert migration_audits(storage)[0]["status"] == "installed_audit_failed"
    storage.recover_legacy_imports(TARGET_SESSION_ID)
    assert artifact_manifest(storage.root(TARGET_SESSION_ID)) == before


def test_source_change_before_install_is_detected(
    migration_setup: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = migration_setup()
    validate = migration_store._validate_staging

    def mutate(*args: object) -> None:
        validate(*args)
        with storage.jsonl_path(SOURCE_SESSION_ID).open("ab") as stream:
            stream.write(b"concurrent source change")

    monkeypatch.setattr(migration_store, "_validate_staging", mutate)
    with pytest.raises(RuntimeError, match="source-mismatch"):
        storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
    assert not storage.root(TARGET_SESSION_ID).exists()


@pytest.mark.parametrize("filename", ["index.sqlite", "rollout.jsonl"])
def test_recovery_rejects_replaced_artifact_symlink(
    migration_setup: Callable,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    storage = migration_setup()
    install = migration_store.install_directory

    def fail_after(staging: Path, target: Path) -> None:
        install(staging, target)
        raise OSError("injected post-rename failure")

    monkeypatch.setattr(migration_store, "install_directory", fail_after)
    with pytest.raises(OSError, match="post-rename"):
        storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
    target_file = storage.root(TARGET_SESSION_ID) / filename
    external = storage.root(TARGET_SESSION_ID).parent / f"external-{filename}"
    target_file.rename(external)
    target_file.symlink_to(external)
    original = external.read_bytes()
    audit_before = migration_audits(storage)
    with pytest.raises(RuntimeError, match="符号链接"):
        storage.recover_legacy_imports(TARGET_SESSION_ID)
    assert external.read_bytes() == original
    assert migration_audits(storage) == audit_before


@pytest.mark.parametrize("field", ["status", "outcome"])
def test_final_marker_cannot_hide_failed_message_state(
    migration_setup: Callable, field: str
) -> None:
    records = _accepted_records()
    records[1]["message"]["data"][field] = "failed"
    storage = migration_setup(records)
    result = storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
    assert result["migrated"][0]["status"] == "unknown"
    assert result["migrated"][0]["final_item_id"] is None
    assert result["lossless"] is False


def test_unmapped_metadata_is_protected_and_explicitly_lossy(
    migration_setup: Callable,
) -> None:
    records = _accepted_records()
    secret = "source-only-private-metadata"
    records[0]["metadata"]["private_extension"] = secret
    storage = migration_setup(records)
    original = artifact_manifest(storage.root(SOURCE_SESSION_ID))
    result = storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
    assert result["lossless"] is False
    assert "metadata.private_extension" in json.dumps(result["loss"])
    assert secret not in json.dumps(result)
    assert secret.encode() not in storage.jsonl_path(TARGET_SESSION_ID).read_bytes()
    assert secret.encode() not in storage.index_path(TARGET_SESSION_ID).read_bytes()
    assert artifact_manifest(storage.root(SOURCE_SESSION_ID)) == original


@pytest.mark.parametrize(
    "corruption",
    [
        "overlap",
        "gap",
        "trailing_committed",
        "bad_hash",
        "duplicate_key",
        "missing_turn",
        "bad_type",
        "unfinished_line",
    ],
)
def test_strict_manifest_envelope_coordinates(
    migration_setup: Callable, corruption: str
) -> None:
    records = _accepted_records()
    if corruption == "bad_hash":
        records[0]["payload_hash"] = "sha256:jcs:v1:" + "0" * 64
    if corruption == "missing_turn":
        del records[0]["turn_id"]
    if corruption == "bad_type":
        records[0]["record_type"] = "item"
    storage = migration_setup(records)
    if corruption in {"overlap", "gap"}:
        with sqlite3.connect(storage.index_path(SOURCE_SESSION_ID)) as connection:
            connection.execute(
                "UPDATE messages SET jsonl_offset=jsonl_offset+? WHERE message_sequence=2",
                (-1 if corruption == "overlap" else 1,),
            )
    if corruption == "trailing_committed":
        with sqlite3.connect(storage.index_path(SOURCE_SESSION_ID)) as connection:
            connection.execute("DELETE FROM messages WHERE message_sequence=2")
    if corruption in {"duplicate_key", "unfinished_line"}:
        lines = storage.jsonl_path(SOURCE_SESSION_ID).read_bytes().splitlines(keepends=True)
        lines[0] = (
            lines[0].replace(
                b'{"format_version":1,', b'{"format_version":1,"format_version":1,'
            )
            if corruption == "duplicate_key"
            else lines[0].rstrip(b"\n")
        )
        storage.jsonl_path(SOURCE_SESSION_ID).write_bytes(b"".join(lines))
        with sqlite3.connect(storage.index_path(SOURCE_SESSION_ID)) as connection:
            connection.execute(
                "UPDATE messages SET jsonl_length=? WHERE message_sequence=1",
                (len(lines[0]),),
            )
            connection.execute(
                "UPDATE messages SET jsonl_offset=? WHERE message_sequence=2",
                (len(lines[0]),),
            )
            connection.execute(
                "UPDATE database_meta SET committed_jsonl_offset=?",
                (sum(map(len, lines)),),
            )
    before = artifact_manifest(storage.root(SOURCE_SESSION_ID))
    with pytest.raises((FormatDispatchError, RuntimeError)):
        storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
    assert artifact_manifest(storage.root(SOURCE_SESSION_ID)) == before
    assert not storage.root(TARGET_SESSION_ID).exists()


def test_multiple_final_markers_converge_unknown(migration_setup: Callable) -> None:
    records = _accepted_records()
    extra = copy.deepcopy(records[1])
    extra.update(message_id="assistant-2", message_sequence=3)
    extra["message"]["data"]["id"] = "assistant-2"
    storage = migration_setup([*records, extra])
    result = storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
    assert result["migrated"][0]["status"] == "unknown"
    assert result["migrated"][0]["final_item_id"] is None


def test_legacy_hashes_and_source_coordinates_are_frozen(
    migration_setup: Callable, session_bundle_factory
) -> None:
    records = _accepted_records()
    for record in records:
        record["turn_id"] = None
    storage = migration_setup(records)
    coordinate = {
        "source_session_id": SOURCE_SESSION_ID,
        "message_sequence": 1,
        "message_id": "legacy-u1",
    }
    message_hash = sha256_jcs(
        {**coordinate, "role": "user", "message": records[0]["message"]}
    )
    seed = sha256_jcs({**coordinate, "legacy_message_hash": message_hash})
    candidate = storage.legacy_migration_report(SOURCE_SESSION_ID)["candidates"][0]
    assert candidate["candidate_key"] == f"legacy-missing-turn:{message_hash}"
    assert candidate["legacy_seed_hash"] == seed
    first = storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)[
        "migrated"
    ][0]
    session_bundle_factory(storage.sessions_dir, OTHER_TARGET_SESSION_ID)
    second = storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=OTHER_TARGET_SESSION_ID)[
        "migrated"
    ][0]
    assert first["source_accepted_ingress_id"] == f"legacy-ingress:{seed}"
    assert first["source_initial_execution_id"] == f"legacy-execution:{seed}"
    for field in (
        "target_turn_id",
        "root_item_id",
        "target_accepted_ingress_id",
        "target_acceptance_idempotency_key",
        "target_initial_execution_id",
    ):
        assert first[field] != second[field]
    root = storage.read_items(TARGET_SESSION_ID)[0]
    assert root.metadata["legacy_source_ref"]["message_id"] == "legacy-u1"
    assert root.metadata["legacy_seed_hash"] == seed


@pytest.mark.parametrize(
    "kind",
    [
        "unknown_role",
        "wrong_carrier",
        "protected_reasoning",
        "tool_set_contribution",
        "request_context",
        "provider_metadata",
    ],
)
def test_raw_quarantine_is_complete_private_and_reported(
    migration_setup: Callable, kind: str
) -> None:
    records = _accepted_records()
    raw = records[1]
    secret = "protected-source-body-should-not-leak"
    if kind == "unknown_role":
        raw["role"] = "unknown-role"
    elif kind == "wrong_carrier":
        raw["message"]["type"] = "tool"
    elif kind == "protected_reasoning":
        raw["message"]["data"]["content"] = [
            {"type": "reasoning", "encrypted_content": secret}
        ]
    elif kind == "tool_set_contribution":
        raw["metadata"] = {"contribution_kind": "tool_set"}
    elif kind == "request_context":
        raw["role"] = "system"
        raw["message"]["type"] = raw["message"]["data"]["type"] = "system"
        raw["message"]["data"]["content"] = secret
    else:
        raw["message"]["data"]["additional_kwargs"] = {"protected_prompt": secret}
    storage = migration_setup(records)
    original = storage.jsonl_path(SOURCE_SESSION_ID).read_bytes()
    result = storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
    snapshot = (
        storage.root(TARGET_SESSION_ID).parent / result["raw_artifact_ref"] / "rollout.jsonl"
    )
    assert snapshot.read_bytes() == original
    assert snapshot.stat().st_mode & 0o077 == 0
    assert secret not in json.dumps(result)
    assert secret.encode() not in storage.jsonl_path(TARGET_SESSION_ID).read_bytes()
    assert secret.encode() not in storage.index_path(TARGET_SESSION_ID).read_bytes()
    if kind not in {"request_context", "provider_metadata"}:
        assert result["rejected"][0]["candidate_status"] == "legacy_unsupported_role"
        assert len(result["rejected"][0]["records"]) == 2
        assert storage.read_items(TARGET_SESSION_ID) == []
    elif kind == "provider_metadata":
        assert result["migration_quality"] == "partial"
        assert result["lossless"] is False
    else:
        assert (
            result["candidates"][0]["records"][1]["disposition"]
            == "legacy_request_context"
        )
        assert len(storage.read_items(TARGET_SESSION_ID)) == 1


def test_full_copy_lossless_gate_preserves_overlay_raw_and_refuses_install(
    migration_setup: Callable,
) -> None:
    storage = migration_setup()
    with sqlite3.connect(storage.index_path(SOURCE_SESSION_ID)) as connection:
        connection.executescript(
            "CREATE TABLE source_overlays(base TEXT, delta TEXT); INSERT INTO source_overlays VALUES ('old base','new delta');"
        )
    original = storage.index_path(SOURCE_SESSION_ID).read_bytes()
    with pytest.raises(RuntimeError, match="v1_full_copy_not_lossless"):
        storage.migrate_legacy_to_v2(
            SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID, require_lossless=True
        )
    assert not storage.root(TARGET_SESSION_ID).exists()
    assert storage.index_path(SOURCE_SESSION_ID).read_bytes() == original
    report = migration_audits(storage)[0]
    assert report["result"]["lossless"] is False
    assert report["result"]["loss"][0]["tables"] == {"source_overlays": 1}


@pytest.mark.parametrize("corruption", ["offset", "final", "view"])
def test_corrupt_staging_is_not_initialized_as_recovered_target(
    migration_setup: Callable,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    storage = migration_setup()
    validate = migration_store._validate_staging

    def corrupt(staged: LegacyMigrationStorage, target: str, namespace: str) -> None:
        queries = {
            "offset": "UPDATE database_meta SET committed_jsonl_offset=0",
            "final": "UPDATE turn_records SET final_item_id=root_input_item_id",
            "view": "DELETE FROM context_view_items",
        }
        with sqlite3.connect(staged.index_path(target)) as connection:
            connection.execute(queries[corruption])
        validate(staged, target, namespace)

    monkeypatch.setattr(migration_store, "_validate_staging", corrupt)
    original = artifact_manifest(storage.root(SOURCE_SESSION_ID))
    with pytest.raises(RuntimeError):
        storage.migrate_legacy_to_v2(SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID)
    assert not storage.root(TARGET_SESSION_ID).exists()
    assert artifact_manifest(storage.root(SOURCE_SESSION_ID)) == original
    assert migration_audits(storage)[0]["status"] == "failed"
