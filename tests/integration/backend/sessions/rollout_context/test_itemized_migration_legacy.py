"""真实 v1 message-line 到 v2 rollout 的一次性迁移集成验收。"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.domain.itemized.errors import FormatDispatchError
from app.services.infrastructure.rollout_context.migration import (
    store as migration_store,
)
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
)
from app.services.infrastructure.rollout_context.migration.dispatch import (
    require_v2_runtime,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.itemized_migration_helpers import (
    _accepted_records,
    _storage,
    _write_v1_source,
    prepare_migration_workspace,
)


@pytest.fixture
def migration_setup(request: pytest.FixtureRequest, session_bundle_factory):
    """每个 case 使用正式 output 下独立的 source/target session。"""
    workspace = prepare_migration_workspace(request)
    sessions = workspace / ".boxteam" / "sessions"

    def setup(records: list[dict[str, object]] | None = None) -> SimpleNamespace:
        source_id = f"ses_{uuid4().hex}"
        target_id = f"ses_{uuid4().hex}"
        lines = _write_v1_source(
            sessions,
            session_id=source_id,
            session_bundle_factory=session_bundle_factory,
            records=records if records is not None else _accepted_records(),
        )
        session_bundle_factory(sessions, target_id)
        storage = _storage(sessions)
        return SimpleNamespace(
            workspace=workspace,
            sessions=sessions,
            source_id=source_id,
            target_id=target_id,
            storage=storage,
            source_root=storage.root(source_id),
            target_root=storage.root(target_id),
            source_lines=lines,
        )

    return setup


def _audit_root(case: SimpleNamespace) -> Path:
    return case.target_root.parent / "legacy-import"


def _audit_records(case: SimpleNamespace) -> list[dict[str, object]]:
    return [
        json.loads(path.read_bytes())
        for path in sorted(_audit_root(case).glob("*/report.json"))
    ]


def _audit_path(case: SimpleNamespace) -> Path:
    paths = sorted(_audit_root(case).glob("*/report.json"))
    assert len(paths) == 1
    return paths[0].parent


def test_public_v1_report_is_sanitized_and_v2_runtime_is_strict(
    migration_setup,
) -> None:
    records = _accepted_records()
    secret = "legacy-provider-secret-that-must-stay-in-source"
    records[0]["message"]["data"]["additional_kwargs"] = {"private": secret}
    case = migration_setup(records)
    source_before = artifact_manifest(case.source_root)

    report = case.storage.legacy_migration_report(case.source_id)
    assert report["read_only"] is True
    assert report["source_format_version"] == 1
    assert report["target_format_version"] == 2
    assert report["dispatch_contract"] == {
        "source_reader": "legacy_import_v1_to_v2_only",
        "target_runtime_format_version": 2,
        "v1_runtime_fallback": False,
        "source_coordinates_are_audit_only": True,
    }
    assert secret not in json.dumps(report, ensure_ascii=False)
    assert report["candidates"][0]["records"][0]["protection"] == "protected"
    with pytest.raises(FormatDispatchError, match="v1_migration_required"):
        case.storage.read_items(case.source_id)
    require_v2_runtime(2)
    with pytest.raises(FormatDispatchError, match="v1_migration_required"):
        require_v2_runtime(1)
    with pytest.raises(FormatDispatchError, match="unsupported_rollout_format_version"):
        require_v2_runtime(9)

    result = case.storage.migrate_legacy_to_v2(
        case.source_id, target_thread_id=case.target_id
    )
    assert secret not in json.dumps(result, ensure_ascii=False)
    assert case.storage.read_items(case.target_id)[0].payload == "迁移前的请求"
    assert artifact_manifest(case.source_root) == source_before
    assert case.storage.index_path(case.source_id).read_bytes() == (
        case.source_root / "index.sqlite"
    ).read_bytes()


def test_success_installs_only_v2_and_preserves_final_lineage(
    migration_setup,
) -> None:
    case = migration_setup()
    source_before = artifact_manifest(case.source_root)

    result = case.storage.migrate_legacy_to_v2(
        case.source_id, target_thread_id=case.target_id
    )

    assert result["status"] == "completed"
    assert result["lossless"] is True
    assert result["rejected"] == []
    items = case.storage.read_items(case.target_id)
    assert [item.payload for item in items] == ["迁移前的请求", "迁移后的回答"]
    assert [item.producer_ref["producer_kind"] for item in items] == [
        "user",
        "provider",
    ]
    assert all(item.item_id not in {"legacy-u1", "legacy-a1"} for item in items)
    assert items[0].metadata["legacy_source_ref"] == {
        "session_id": case.source_id,
        "message_sequence": 1,
        "message_id": "legacy-u1",
    }
    assert result["migrated"][0]["final_item_id"] == items[1].item_id
    with case.storage._connect(case.target_id, "", read_only=True) as connection:
        assert connection.execute(
            "SELECT rollout_format_version,database_state FROM database_meta WHERE singleton_id=1"
        ).fetchone() == (2, "active")
        assert connection.execute(
            "SELECT status FROM turn_records"
        ).fetchone() == ("completed",)
        report_json = connection.execute(
            "SELECT report_json FROM legacy_migration_reports"
        ).fetchone()[0]
    report = json.loads(report_json)
    assert report["source_format_version"] == 1
    assert report["target_format_version"] == 2
    assert artifact_manifest(case.source_root) == source_before
    for line in (case.target_root / "rollout.jsonl").read_bytes().splitlines():
        envelope = json.loads(line)
        assert envelope["format_version"] == 2
        assert envelope["record_type"] == "item"


def test_missing_final_marker_is_unknown_and_has_no_final_item(migration_setup) -> None:
    records = _accepted_records()
    records[1]["metadata"] = {}
    case = migration_setup(records)

    result = case.storage.migrate_legacy_to_v2(
        case.source_id, target_thread_id=case.target_id
    )

    assert result["migrated"][0]["status"] == "unknown"
    assert result["migrated"][0]["final_item_id"] is None
    with case.storage._connect(case.target_id, "", read_only=True) as connection:
        assert connection.execute(
            "SELECT status,final_item_id FROM turn_records"
        ).fetchone() == ("unknown", None)
        assert connection.execute("SELECT outcome FROM executions").fetchone() == (
            "unknown",
        )


def test_rejected_candidate_is_quarantined_without_lossless_claim(migration_setup) -> None:
    records = _accepted_records()
    rejected = copy.deepcopy(records[1])
    rejected.update(message_sequence=3, message_id="legacy-unknown", role="alien")
    rejected["message"]["data"]["id"] = "legacy-unknown"
    case = migration_setup([*records, rejected])
    source_before = artifact_manifest(case.source_root)

    result = case.storage.migrate_legacy_to_v2(
        case.source_id, target_thread_id=case.target_id
    )

    assert result["status"] == "completed_with_rejections"
    assert result["lossless"] is False
    assert result["migrated"] == []
    assert result["rejected"][0]["candidate_status"] == "legacy_unsupported_role"
    audit = _audit_path(case)
    quarantine = json.loads((audit / "quarantine.json").read_bytes())
    assert quarantine["schema"] == "legacy-import-quarantine:v1"
    assert quarantine["candidate_count"] == 1
    assert quarantine["candidates"][0]["records"][-1]["message_id"] == (
        "legacy-unknown"
    )
    assert quarantine["candidates"][0]["records"][-1]["role"] == "alien"
    assert "legacy-provider-secret" not in json.dumps(quarantine, ensure_ascii=False)
    assert artifact_manifest(case.source_root) == source_before
    assert json.loads((case.target_root / "legacy-import.json").read_bytes())[
        "target_session_id"
    ] == case.target_id
    assert len(case.storage.read_items(case.target_id)) == 0


def test_require_lossless_failure_keeps_source_snapshot_and_staging_audit(
    migration_setup,
) -> None:
    records = _accepted_records()
    rejected = copy.deepcopy(records[1])
    rejected.update(message_sequence=3, message_id="legacy-rejected", role="alien")
    rejected["message"]["data"]["id"] = "legacy-rejected"
    case = migration_setup([*records, rejected])
    source_before = artifact_manifest(case.source_root)

    with pytest.raises(RuntimeError, match="v1_full_copy_not_lossless"):
        case.storage.migrate_legacy_to_v2(
            case.source_id, target_thread_id=case.target_id, require_lossless=True
        )

    assert not case.target_root.exists()
    audit, = _audit_records(case)
    assert audit["status"] == "failed"
    assert audit["rollback"] == "uninstalled_staging_quarantined"
    assert (next(_audit_root(case).glob("*/source")) / "rollout.jsonl").exists()
    assert artifact_manifest(case.source_root) == source_before


def test_precommit_validation_failure_never_installs_target(migration_setup, monkeypatch) -> None:
    case = migration_setup()
    source_before = artifact_manifest(case.source_root)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected migration staging validation failure")

    monkeypatch.setattr(migration_store, "_validate_staging", fail)
    with pytest.raises(RuntimeError, match="staging validation failure"):
        case.storage.migrate_legacy_to_v2(
            case.source_id, target_thread_id=case.target_id
        )

    assert not case.target_root.exists()
    audit, = _audit_records(case)
    assert audit["status"] == "failed"
    assert audit["rollback"] == "uninstalled_staging_quarantined"
    assert (next(_audit_root(case).glob("*/staging")) / "index.sqlite").exists()
    assert artifact_manifest(case.source_root) == source_before


@pytest.mark.parametrize("phase", ["during_build", "after_install"])
def test_restart_recovers_only_audit_and_then_installs_v2(
    migration_setup,
    request: pytest.FixtureRequest,
    phase: str,
) -> None:
    case = migration_setup()
    source_before = artifact_manifest(case.source_root)
    script = """
import os
import sys
from unittest.mock import patch
from app.services.infrastructure.rollout_context.migration import store
from app.services.infrastructure.rollout_context.execution.executions import RolloutExecutionsMixin
from tests.integration.backend.sessions.itemized_migration_helpers import _storage

phase = sys.argv[4]
if phase == "during_build":
    owner, method = RolloutExecutionsMixin, "converge_execution"
    def interrupt(*args, **kwargs):
        os._exit(73)
else:
    owner, method = store, "install_directory"
    original = store.install_directory
    def interrupt(staging, target):
        original(staging, target)
        os._exit(73)
with patch.object(owner, method, interrupt):
    _storage(sys.argv[1]).migrate_legacy_to_v2(sys.argv[2], target_thread_id=sys.argv[3])
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(case.sessions),
            case.source_id,
            case.target_id,
            phase,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        cwd=Path.cwd(),
    )
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / f"{phase}.stdout.log").write_text(process.stdout)
    (context.artifacts_dir / f"{phase}.stderr.log").write_text(process.stderr)
    assert process.returncode == 73, process.stderr
    assert artifact_manifest(case.source_root) == source_before

    recovered = case.storage.recover_legacy_imports(case.target_id)
    assert recovered[0]["status"] == (
        "failed" if phase == "during_build" else "installed"
    )
    if phase == "after_install":
        target_before = artifact_manifest(case.target_root)
        assert len(case.storage.read_items(case.target_id)) == 2
        target_after = artifact_manifest(case.target_root)
        for name in ("rollout.jsonl", "index.sqlite"):
            assert target_after[name] == target_before[name]
    else:
        assert not case.target_root.exists()
        case.storage.migrate_legacy_to_v2(
            case.source_id, target_thread_id=case.target_id
        )
        assert len(case.storage.read_items(case.target_id)) == 2
    assert artifact_manifest(case.source_root) == source_before


@pytest.mark.parametrize("location", ["source_jsonl", "source_root", "target_root", "audit_root"])
def test_symlink_boundaries_are_rejected_before_external_mutation(migration_setup, location):
    case = migration_setup()
    external = case.target_root.parent / f"external-{uuid4().hex}"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_bytes(b"must remain unchanged")
    paths = {
        "source_jsonl": case.source_root / "rollout.jsonl",
        "source_root": case.source_root,
        "target_root": case.target_root,
        "audit_root": _audit_root(case),
    }
    path = paths[location]
    if path.exists():
        path.rename(path.with_name(path.name + ".original"))
    path.symlink_to(external if location.endswith("root") else sentinel)
    before = artifact_manifest(external)

    with pytest.raises(RuntimeError, match="符号链接"):
        case.storage.migrate_legacy_to_v2(
            case.source_id, target_thread_id=case.target_id
        )
    assert artifact_manifest(external) == before


def test_hardlink_source_is_rejected_without_creating_target(migration_setup) -> None:
    case = migration_setup()
    os.link(case.source_root / "rollout.jsonl", case.source_root / "linked.jsonl")

    with pytest.raises(RuntimeError, match="硬链接"):
        case.storage.migrate_legacy_to_v2(
            case.source_id, target_thread_id=case.target_id
        )
    assert not case.target_root.exists()


def test_uncommitted_tail_and_overlay_rows_are_raw_loss_not_v2_history(migration_setup) -> None:
    case = migration_setup()
    source_jsonl = case.source_root / "rollout.jsonl"
    tail = b'{"format_version":1,"record_type":"message","message_sequence":99}\n'
    source_jsonl.write_bytes(source_jsonl.read_bytes() + tail)
    with sqlite3.connect(case.source_root / "index.sqlite") as connection:
        connection.execute("CREATE TABLE overlay_state(base_epoch TEXT, delta_json TEXT)")
        connection.execute("INSERT INTO overlay_state VALUES ('base-1','delta-1')")
        connection.commit()
    source_before = artifact_manifest(case.source_root)

    result = case.storage.migrate_legacy_to_v2(
        case.source_id, target_thread_id=case.target_id
    )

    assert result["lossless"] is False
    reasons = {entry["reason"] for entry in result["loss"]}
    assert "uncommitted_tail_quarantined" in reasons
    assert "legacy_sqlite_state_preserved_raw" in reasons
    assert len(case.storage.read_items(case.target_id)) == 2
    assert tail not in (case.target_root / "rollout.jsonl").read_bytes()
    assert artifact_manifest(case.source_root) == source_before


def test_manifest_envelope_mismatch_fails_with_original_and_audit_preserved(
    migration_setup,
) -> None:
    case = migration_setup()
    source_jsonl = case.source_root / "rollout.jsonl"
    lines = source_jsonl.read_bytes().splitlines(keepends=True)
    lines[0] = lines[0].replace(b'"format_version":1', b'"format_version":9', 1)
    source_jsonl.write_bytes(b"".join(lines))
    source_before = artifact_manifest(case.source_root)

    with pytest.raises(FormatDispatchError, match="format_version"):
        case.storage.migrate_legacy_to_v2(
            case.source_id, target_thread_id=case.target_id
        )

    assert not case.target_root.exists()
    audit, = _audit_records(case)
    assert audit["status"] == "failed"
    assert audit["rollback"] == "uninstalled_staging_quarantined"
    assert artifact_manifest(case.source_root) == source_before
