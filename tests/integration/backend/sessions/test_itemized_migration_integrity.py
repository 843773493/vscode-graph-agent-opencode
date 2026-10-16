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
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.migration import (
    importer as migration_importer,
)
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
    write_private,
)
from app.services.infrastructure.rollout_context.migration.importer import (
    LegacyRolloutImporter,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.itemized_migration_helpers import (
    _accepted_records,
    _storage,
    _write_v1_source,
    migration_audits,
    prepare_migration_workspace,
)

SOURCE_SESSION_ID = "ses_019c205391bb7a4b81b6728df70f63e6"
TARGET_SESSION_ID = "ses_019b9187b87f78c78ef242bbd6043bfc"
OTHER_TARGET_SESSION_ID = "ses_019bf2257d7e7fd38db6585cdd161690"


@pytest.fixture
def migration_setup(request: pytest.FixtureRequest, session_bundle_factory) -> Callable:
    workspace = prepare_migration_workspace(request)
    sessions = workspace / ".boxteam" / "sessions"

    def setup(records: list[dict[str, object]] | None = None) -> RolloutContextStore:
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
    target = storage.storage.root(TARGET_SESSION_ID)
    if existing == "empty_v2":
        storage.storage.initialize(TARGET_SESSION_ID)
    else:
        target.mkdir()
        if existing == "partial":
            (target / "rollout.jsonl").write_bytes(b"preexisting uncommitted data")
    before = artifact_manifest(target)
    if existing == "empty_directory":
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
    else:
        with pytest.raises(RuntimeError, match="空 rollout"):
            LegacyRolloutImporter(storage).import_v1(
                source_session_id=SOURCE_SESSION_ID,
                source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
                target_session_id=TARGET_SESSION_ID,
                target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
                checkpoint_ns="",
            )
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
    external = storage.storage.root(TARGET_SESSION_ID).parent / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_bytes(b"must not change")
    paths = {
        "source_jsonl": storage.storage.jsonl_path(SOURCE_SESSION_ID),
        "source_index": storage.storage.index_path(SOURCE_SESSION_ID),
        "source_root": storage.storage.root(SOURCE_SESSION_ID),
        "target_root": storage.storage.root(TARGET_SESSION_ID),
        "audit_root": storage.storage.root(TARGET_SESSION_ID).parent / "legacy-import",
        "target_lock": storage.storage.root(TARGET_SESSION_ID).parent / ".rollout.write.lock",
    }
    path = paths[location]
    if path.exists():
        path.rename(path.with_name(path.name + ".original"))
    path.symlink_to(external if location.endswith("root") else sentinel)
    before = artifact_manifest(external)
    with pytest.raises(RuntimeError, match="符号链接|symlink"):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    assert artifact_manifest(external) == before


def test_hardlink_source_is_rejected(migration_setup: Callable) -> None:
    storage = migration_setup()
    os.link(
        storage.storage.jsonl_path(SOURCE_SESSION_ID),
        storage.storage.root(SOURCE_SESSION_ID) / "linked.jsonl",
    )
    with pytest.raises(RuntimeError, match="硬链接"):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    assert not storage.storage.root(TARGET_SESSION_ID).exists()


@pytest.mark.parametrize("phase", ["during_build", "before_install", "after_install"])
def test_process_exit_preserves_originals_and_recovers_only_audit(
    migration_setup: Callable,
    request: pytest.FixtureRequest,
    phase: str,
) -> None:
    storage = migration_setup()
    before = artifact_manifest(storage.storage.root(SOURCE_SESSION_ID))
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    script = """
import os
import sys
from unittest.mock import patch
from app.services.infrastructure.rollout_context.migration import importer
from app.services.infrastructure.rollout_context.migration.importer import LegacyRolloutImporter
from tests.support.session_catalog_owner import fixture_catalog_scope
from tests.integration.backend.sessions.itemized_migration_helpers import _storage
from app.services.infrastructure.rollout_context.checkpoint.projection.message_projections import RolloutMessageProjectionMixin
original = importer.install_directory
def crash(staging, target):
    if sys.argv[2] == "after_install":
        original(staging, target)
    os._exit(73)
owner, method = (RolloutMessageProjectionMixin, "_materialize_canonical_message_projection") if sys.argv[2] == "during_build" else (importer, "install_directory")
def exit_during_build(*args, **kwargs):
    os._exit(73)
with patch.object(owner, method, exit_during_build if sys.argv[2] == "during_build" else crash):
    with fixture_catalog_scope():
        storage = _storage(sys.argv[1])
        LegacyRolloutImporter(storage).import_v1(source_session_id=sys.argv[3], source_thread_id=storage.storage.main_thread_id(sys.argv[3]), target_session_id=sys.argv[4], target_thread_id=storage.storage.main_thread_id(sys.argv[4]), checkpoint_ns="")
"""
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
            "-c",
            script,
            str(storage.storage.sessions_dir),
            phase,
            SOURCE_SESSION_ID,
            TARGET_SESSION_ID,
        ],
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    (context.artifacts_dir / f"{phase}.log").write_text(process.stdout + process.stderr)
    assert process.returncode == 73, process.stderr
    assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == before
    assert migration_audits(storage)[0]["status"] == (
        "running" if phase == "during_build" else "ready"
    )
    installed = phase == "after_install"
    target_before = (
        artifact_manifest(storage.storage.root(TARGET_SESSION_ID)) if installed else None
    )
    recovered = LegacyRolloutImporter(_storage(storage.storage.sessions_dir)).recover(
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=_storage(storage.storage.sessions_dir).storage.main_thread_id(
            TARGET_SESSION_ID
        ),
        checkpoint_ns="",
    )
    assert recovered[0]["status"] == ("installed" if installed else "failed")
    if installed:
        assert artifact_manifest(storage.storage.root(TARGET_SESSION_ID)) == target_before
        assert (
            len(
                storage.storage.read_items(
                    TARGET_SESSION_ID, storage.storage.main_thread_id(TARGET_SESSION_ID)
                )
            )
            == 2
        )
    else:
        assert not storage.storage.root(TARGET_SESSION_ID).exists()
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
        assert [report["status"] for report in migration_audits(storage)].count(
            "installed"
        ) == 1


@pytest.mark.parametrize(
    ("phase", "entry"),
    [
        ("before_initial_report", "recover_then_import"),
        ("before_initial_report", "import_direct"),
        ("initial_report_before_replace", "recover_then_import"),
        ("initial_report_before_replace", "import_direct"),
        ("initial_report_before_replace", "recover_exit_before_initial_replace"),
        ("initial_report_before_replace", "recover_exit_before_failed_replace"),
        ("initial_report_empty", "recover_then_import"),
        ("initial_report_empty", "import_direct"),
        ("initial_report_truncated", "recover_then_import"),
        ("initial_report_truncated", "import_direct"),
        ("initial_report_utf8_truncated", "recover_then_import"),
        ("initial_report_utf8_truncated", "import_direct"),
        ("initial_report_empty", "recover_exit_after_partial_unlink"),
        ("initial_report_truncated", "recover_exit_after_partial_unlink"),
    ],
)
def test_initial_audit_process_exit_does_not_block_fresh_import(
    migration_setup: Callable,
    request: pytest.FixtureRequest,
    phase: str,
    entry: str,
) -> None:
    storage = migration_setup()
    source_before = artifact_manifest(storage.storage.root(SOURCE_SESSION_ID))
    namespace = "迁移审计" if phase == "initial_report_utf8_truncated" else ""
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    script = """
import os
import sys
from unittest.mock import patch
from app.services.infrastructure.rollout_context.migration import artifacts, importer
from app.services.infrastructure.rollout_context.migration.importer import LegacyRolloutImporter
from tests.support.session_catalog_owner import fixture_catalog_scope
from tests.integration.backend.sessions.itemized_migration_helpers import _storage
def crash(*args, **kwargs):
    if sys.argv[2] == "initial_report_before_replace":
        assert args[0].name.endswith(".next.json") and args[1].name == "report.json"
        assert args[0].is_file() and not args[1].exists()
    os._exit(73)
write = artifacts.write_private
def crash_during_write(path, data):
    if not path.name.endswith(".next.json"):
        return write(path, data)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    if sys.argv[2] == "initial_report_truncated":
        prefix = data[:len(data) // 2]
        assert os.write(descriptor, prefix) == len(prefix)
    elif sys.argv[2] == "initial_report_utf8_truncated":
        prefix = data[:data.index("迁".encode()) + 1]
        assert os.write(descriptor, prefix) == len(prefix)
    os._exit(73)
if sys.argv[2] in {"initial_report_empty", "initial_report_truncated", "initial_report_utf8_truncated"}:
    owner, method, replacement = artifacts, "write_private", crash_during_write
else:
    owner, method = (importer, "write_audit") if sys.argv[2] == "before_initial_report" else (artifacts.os, "replace")
    replacement = crash
with patch.object(owner, method, replacement):
    with fixture_catalog_scope():
        storage = _storage(sys.argv[1])
        LegacyRolloutImporter(storage).import_v1(source_session_id=sys.argv[3], source_thread_id=storage.storage.main_thread_id(sys.argv[3]), target_session_id=sys.argv[4], target_thread_id=storage.storage.main_thread_id(sys.argv[4]), checkpoint_ns=sys.argv[5])
"""
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
            "-c",
            script,
            str(storage.storage.sessions_dir),
            phase,
            SOURCE_SESSION_ID,
            TARGET_SESSION_ID,
            namespace,
        ],
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    (context.artifacts_dir / f"{phase}-{entry}.log").write_text(
        process.stdout + process.stderr
    )
    assert process.returncode == 73, process.stderr
    audit_root = storage.storage.root(TARGET_SESSION_ID).parent / "legacy-import"
    (audit,) = audit_root.iterdir()
    assert not (audit / "report.json").exists()
    pending = list(audit.iterdir())
    assert len(pending) == (phase != "before_initial_report")
    initial_record = (
        json.loads(pending[0].read_text())
        if phase == "initial_report_before_replace"
        else None
    )
    if phase in {
        "initial_report_empty",
        "initial_report_truncated",
        "initial_report_utf8_truncated",
    }:
        partial = pending[0].read_bytes()
        assert bool(partial) == (phase != "initial_report_empty")
        error_type = (
            UnicodeDecodeError
            if phase == "initial_report_utf8_truncated"
            else json.JSONDecodeError
        )
        with pytest.raises(error_type):
            json.loads(partial)
        (context.artifacts_dir / f"{phase}-{entry}.initial-next.txt").write_bytes(
            partial
        )
    assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == source_before
    assert not storage.storage.root(TARGET_SESSION_ID).exists()
    restart_script = """
import os
import sys
from pathlib import Path
from unittest.mock import patch
from app.services.infrastructure.rollout_context.migration import importer
from app.services.infrastructure.rollout_context.migration.artifacts import artifact_manifest
from app.services.infrastructure.rollout_context.migration.importer import LegacyRolloutImporter
from tests.support.session_catalog_owner import fixture_catalog_scope
from tests.integration.backend.sessions.itemized_migration_helpers import _storage
with fixture_catalog_scope():
    storage = _storage(sys.argv[1])
    source, target = sys.argv[3:5]
    original = artifact_manifest(storage.storage.root(source))
    namespace = sys.argv[8]
    if sys.argv[2] == "recover_exit_after_partial_unlink":
        unlink = Path.unlink
        def crash(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if path.name == Path(sys.argv[7]).name:
                os._exit(74)
        with patch.object(Path, "unlink", crash):
            LegacyRolloutImporter(storage).recover(target_session_id=target, target_thread_id=storage.storage.main_thread_id(target), checkpoint_ns=namespace)
        raise AssertionError("recovery 未到达指定删除退出窗口")
    if sys.argv[2].startswith("recover_exit"):
        replace = os.replace
        def crash(source, target):
            initial = sys.argv[2] == "recover_exit_before_initial_replace"
            if initial == (source.name == Path(sys.argv[7]).name):
                os._exit(74)
            replace(source, target)
        with patch.object(importer.os, "replace", crash):
            LegacyRolloutImporter(storage).recover(target_session_id=target, target_thread_id=storage.storage.main_thread_id(target), checkpoint_ns=namespace)
        raise AssertionError("recovery 未到达指定退出窗口")
    if sys.argv[2] == "recover_then_import":
        recovered = LegacyRolloutImporter(storage).recover(target_session_id=target, target_thread_id=storage.storage.main_thread_id(target), checkpoint_ns=namespace)
        if sys.argv[5] == "initial_report_before_replace":
            assert len(recovered) == 1 and recovered[0]["status"] == "failed"
            assert recovered[0]["rollback"] == "uninstalled_staging_quarantined"
        else:
            assert recovered == [] and not Path(sys.argv[6]).exists()
        assert artifact_manifest(storage.storage.root(source)) == original
        assert not storage.storage.root(target).exists()
        assert LegacyRolloutImporter(_storage(sys.argv[1])).recover(target_session_id=target, target_thread_id=storage.storage.main_thread_id(target), checkpoint_ns=namespace) == []
    storage = _storage(sys.argv[1])
    LegacyRolloutImporter(storage).import_v1(source_session_id=source, source_thread_id=storage.storage.main_thread_id(source), target_session_id=target, target_thread_id=storage.storage.main_thread_id(target), checkpoint_ns=namespace)
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
            restart_script,
            str(storage.storage.sessions_dir),
            entry,
            SOURCE_SESSION_ID,
            TARGET_SESSION_ID,
            phase,
            str(audit),
            str(pending[0]) if pending else "",
            namespace,
        ],
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    (context.artifacts_dir / f"{phase}-{entry}-restarted.log").write_text(
        restarted.stdout + restarted.stderr
    )
    if entry.startswith("recover_exit"):
        assert restarted.returncode == 74, restarted.stderr
        assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == source_before
        assert not storage.storage.root(TARGET_SESSION_ID).exists()
        fresh = _storage(storage.storage.sessions_dir)
        recovered = LegacyRolloutImporter(fresh).recover(
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=fresh.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns=namespace,
        )
        if entry == "recover_exit_after_partial_unlink":
            assert recovered == [] and not audit.exists()
        else:
            assert recovered[0]["status"] == "failed"
        LegacyRolloutImporter(_storage(storage.storage.sessions_dir)).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=fresh.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=fresh.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns=namespace,
        )
    else:
        assert restarted.returncode == 0, restarted.stderr
    fresh = _storage(storage.storage.sessions_dir)
    reports = migration_audits(fresh)
    assert [report["status"] for report in reports].count("installed") == 1
    assert [report["status"] for report in reports].count("failed") == (
        initial_record is not None
    )
    if initial_record is not None:
        assert not pending[0].exists()
        recovered_report = json.loads((audit / "report.json").read_text())
        assert {
            key: recovered_report[key] for key in initial_record if key != "status"
        } == {key: value for key, value in initial_record.items() if key != "status"}
    assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == source_before
    assert (
        len(
            fresh.storage.read_items(
                TARGET_SESSION_ID,
                fresh.storage.main_thread_id(TARGET_SESSION_ID),
                checkpoint_ns=namespace,
            )
        )
        == 2
    )


@pytest.mark.parametrize(
    "damage",
    [
        "unknown_file",
        "source_directory",
        "multiple_next",
        "wrong_identity",
        "symlink",
        "hardlink",
        "read_failure",
    ],
)
def test_unpublished_initial_audit_rejects_unknown_and_unreadable_artifacts(
    migration_setup: Callable,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    storage = migration_setup()

    def interrupt(root: Path, record: dict[str, object]) -> None:
        write_private(
            root / f"report-{'0' * 32}.next.json", canonical_json_bytes(record)
        )
        raise OSError("首份审计发布前中断")

    with monkeypatch.context() as patch:
        patch.setattr(migration_importer, "write_audit", interrupt)
        with pytest.raises(OSError, match="发布前中断"):
            LegacyRolloutImporter(storage).import_v1(
                source_session_id=SOURCE_SESSION_ID,
                source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
                target_session_id=TARGET_SESSION_ID,
                target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
                checkpoint_ns="",
            )
    audit_root = storage.storage.root(TARGET_SESSION_ID).parent / "legacy-import"
    (audit,) = audit_root.iterdir()
    (pending,) = audit.iterdir()
    sentinel = audit_root.parent / "audit-sentinel"
    sentinel.write_bytes(b"{")
    error_type: type[Exception] = RuntimeError
    message = "report"
    if damage == "unknown_file":
        (audit / "unknown.json").write_bytes(b"{")
    elif damage == "source_directory":
        (audit / "source").mkdir()
    elif damage == "multiple_next":
        write_private(audit / f"report-{'1' * 32}.next.json", b"{")
    elif damage == "wrong_identity":
        record = json.loads(pending.read_bytes())
        record["target_session_id"] = SOURCE_SESSION_ID
        pending.write_bytes(canonical_json_bytes(record))
        message = "identity"
    elif damage in {"symlink", "hardlink"}:
        pending.unlink()
        if damage == "symlink":
            pending.symlink_to(sentinel)
            message = "符号链接"
        else:
            os.link(sentinel, pending)
            message = "硬链接"
    else:

        def read_failure(path: Path) -> bytes:
            raise OSError(f"读取首份审计失败: {path}")

        monkeypatch.setattr(migration_importer, "read_regular", read_failure)
        error_type, message = OSError, "读取首份审计失败"
    source_before = artifact_manifest(storage.storage.root(SOURCE_SESSION_ID))
    names_before = sorted(path.name for path in audit.iterdir())
    pending_before = pending.read_bytes()
    fresh = _storage(storage.storage.sessions_dir)
    owner = LegacyRolloutImporter(fresh)
    with pytest.raises(error_type, match=message):
        owner.recover(
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=fresh.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    with pytest.raises(error_type, match=message):
        owner.import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=fresh.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=fresh.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    assert sorted(path.name for path in audit.iterdir()) == names_before
    assert pending.read_bytes() == pending_before
    assert pending.is_symlink() == (damage == "symlink")
    assert sentinel.read_bytes() == b"{"
    assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == source_before
    assert not storage.storage.root(TARGET_SESSION_ID).exists()


@pytest.mark.parametrize(
    ("damage", "error_type", "message"),
    [
        ("invalid_json", ValueError, "Expecting"),
        ("identity", RuntimeError, "audit identity"),
        ("missing", RuntimeError, "report"),
    ],
)
def test_published_audit_damage_is_rejected_without_changing_artifacts(
    migration_setup: Callable,
    damage: str,
    error_type: type[Exception],
    message: str,
) -> None:
    storage = migration_setup()
    LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
    )
    audit_root = storage.storage.root(TARGET_SESSION_ID).parent / "legacy-import"
    (audit,) = audit_root.iterdir()
    report_path = audit / "report.json"
    if damage == "invalid_json":
        report_path.write_text("{broken")
    elif damage == "identity":
        report = json.loads(report_path.read_text())
        report["target_session_id"] = SOURCE_SESSION_ID
        report_path.write_text(json.dumps(report))
    else:
        report_path.unlink()
    source_before = artifact_manifest(storage.storage.root(SOURCE_SESSION_ID))
    target_before = artifact_manifest(storage.storage.root(TARGET_SESSION_ID))
    audit_before = artifact_manifest(audit_root)
    fresh = _storage(storage.storage.sessions_dir)
    importer = LegacyRolloutImporter(fresh)
    with pytest.raises(error_type, match=message):
        importer.recover(
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=fresh.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    with pytest.raises(error_type, match=message):
        importer.import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=fresh.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=fresh.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == source_before
    assert artifact_manifest(storage.storage.root(TARGET_SESSION_ID)) == target_before
    assert artifact_manifest(audit_root) == audit_before


def test_post_install_error_cannot_erase_published_history(
    migration_setup: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = migration_setup()
    install = migration_importer.install_directory

    def fail_after(staging: Path, target: Path) -> None:
        install(staging, target)
        raise OSError("injected post-rename failure")

    monkeypatch.setattr(migration_importer, "install_directory", fail_after)
    with pytest.raises(OSError, match="post-rename"):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    before = artifact_manifest(storage.storage.root(TARGET_SESSION_ID))
    assert migration_audits(storage)[0]["status"] == "installed_audit_failed"
    LegacyRolloutImporter(storage).recover(
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
    )
    assert artifact_manifest(storage.storage.root(TARGET_SESSION_ID)) == before


def test_source_change_before_install_is_detected(
    migration_setup: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = migration_setup()
    validate = migration_importer.validate_staging

    def mutate(*args: object) -> None:
        validate(*args)
        with storage.storage.jsonl_path(SOURCE_SESSION_ID).open("ab") as stream:
            stream.write(b"concurrent source change")

    monkeypatch.setattr(migration_importer, "validate_staging", mutate)
    with pytest.raises(RuntimeError, match="source-mismatch"):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    assert not storage.storage.root(TARGET_SESSION_ID).exists()


@pytest.mark.parametrize("filename", ["index.sqlite", "rollout.jsonl"])
def test_recovery_rejects_replaced_artifact_symlink(
    migration_setup: Callable,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    storage = migration_setup()
    install = migration_importer.install_directory

    def fail_after(staging: Path, target: Path) -> None:
        install(staging, target)
        raise OSError("injected post-rename failure")

    monkeypatch.setattr(migration_importer, "install_directory", fail_after)
    with pytest.raises(OSError, match="post-rename"):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    target_file = storage.storage.root(TARGET_SESSION_ID) / filename
    external = storage.storage.root(TARGET_SESSION_ID).parent / f"external-{filename}"
    target_file.rename(external)
    target_file.symlink_to(external)
    original = external.read_bytes()
    audit_before = migration_audits(storage)
    with pytest.raises(RuntimeError, match="符号链接"):
        LegacyRolloutImporter(storage).recover(
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    assert external.read_bytes() == original
    assert migration_audits(storage) == audit_before


@pytest.mark.parametrize("field", ["status", "outcome"])
def test_final_marker_cannot_hide_failed_message_state(
    migration_setup: Callable, field: str
) -> None:
    records = _accepted_records()
    records[1]["message"]["data"][field] = "failed"
    storage = migration_setup(records)
    result = LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
    )
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
    original = artifact_manifest(storage.storage.root(SOURCE_SESSION_ID))
    result = LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
    )
    assert result["lossless"] is False
    assert "metadata.private_extension" in json.dumps(result["loss"])
    assert secret not in json.dumps(result)
    assert secret.encode() not in storage.storage.jsonl_path(TARGET_SESSION_ID).read_bytes()
    assert secret.encode() not in storage.storage.index_path(TARGET_SESSION_ID).read_bytes()
    assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == original


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
        with sqlite3.connect(storage.storage.index_path(SOURCE_SESSION_ID)) as connection:
            connection.execute(
                "UPDATE messages SET jsonl_offset=jsonl_offset+? WHERE message_sequence=2",
                (-1 if corruption == "overlap" else 1,),
            )
    if corruption == "trailing_committed":
        with sqlite3.connect(storage.storage.index_path(SOURCE_SESSION_ID)) as connection:
            connection.execute("DELETE FROM messages WHERE message_sequence=2")
    if corruption in {"duplicate_key", "unfinished_line"}:
        lines = (
            storage.storage.jsonl_path(SOURCE_SESSION_ID).read_bytes().splitlines(keepends=True)
        )
        lines[0] = (
            lines[0].replace(
                b'{"format_version":1,', b'{"format_version":1,"format_version":1,'
            )
            if corruption == "duplicate_key"
            else lines[0].rstrip(b"\n")
        )
        storage.storage.jsonl_path(SOURCE_SESSION_ID).write_bytes(b"".join(lines))
        with sqlite3.connect(storage.storage.index_path(SOURCE_SESSION_ID)) as connection:
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
    before = artifact_manifest(storage.storage.root(SOURCE_SESSION_ID))
    with pytest.raises((FormatDispatchError, RuntimeError)):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == before
    assert not storage.storage.root(TARGET_SESSION_ID).exists()


def test_multiple_final_markers_converge_unknown(migration_setup: Callable) -> None:
    records = _accepted_records()
    extra = copy.deepcopy(records[1])
    extra.update(message_id="assistant-2", message_sequence=3)
    extra["message"]["data"]["id"] = "assistant-2"
    storage = migration_setup([*records, extra])
    result = LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
    )
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
    candidate = LegacyRolloutImporter(storage).read_report(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        checkpoint_ns="",
    )["candidates"][0]
    assert candidate["candidate_key"] == f"legacy-missing-turn:{message_hash}"
    assert candidate["legacy_seed_hash"] == seed
    first = LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
    )["migrated"][0]
    session_bundle_factory(storage.storage.sessions_dir, OTHER_TARGET_SESSION_ID)
    second = LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=OTHER_TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(OTHER_TARGET_SESSION_ID),
        checkpoint_ns="",
    )["migrated"][0]
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
    root = storage.storage.read_items(
        TARGET_SESSION_ID, storage.storage.main_thread_id(TARGET_SESSION_ID)
    )[0]
    assert (
        next(
            relation.target_id
            for relation in root.provenance_relations
            if relation.target_kind == "legacy_message"
        )
        == "legacy-u1"
    )
    assert (
        root.extensions["boxteam.acceptance.annotations"]["value"]["legacy_seed_hash"]
        == seed
    )


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
    original = storage.storage.jsonl_path(SOURCE_SESSION_ID).read_bytes()
    result = LegacyRolloutImporter(storage).import_v1(
        source_session_id=SOURCE_SESSION_ID,
        source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
        checkpoint_ns="",
    )
    snapshot = (
        storage.storage.root(TARGET_SESSION_ID).parent
        / result["raw_artifact_ref"]
        / "rollout.jsonl"
    )
    assert snapshot.read_bytes() == original
    assert snapshot.stat().st_mode & 0o077 == 0
    assert secret not in json.dumps(result)
    assert secret.encode() not in storage.storage.jsonl_path(TARGET_SESSION_ID).read_bytes()
    assert secret.encode() not in storage.storage.index_path(TARGET_SESSION_ID).read_bytes()
    if kind not in {"request_context", "provider_metadata"}:
        assert result["rejected"][0]["candidate_status"] == "legacy_unsupported_role"
        assert len(result["rejected"][0]["records"]) == 2
        assert (
            storage.storage.read_items(
                TARGET_SESSION_ID, storage.storage.main_thread_id(TARGET_SESSION_ID)
            )
            == []
        )
    elif kind == "provider_metadata":
        assert result["migration_quality"] == "partial"
        assert result["lossless"] is False
    else:
        assert (
            result["candidates"][0]["records"][1]["disposition"]
            == "legacy_request_context"
        )
        assert (
            len(
                storage.storage.read_items(
                    TARGET_SESSION_ID, storage.storage.main_thread_id(TARGET_SESSION_ID)
                )
            )
            == 1
        )


def test_full_copy_lossless_gate_preserves_overlay_raw_and_refuses_install(
    migration_setup: Callable,
) -> None:
    storage = migration_setup()
    with sqlite3.connect(storage.storage.index_path(SOURCE_SESSION_ID)) as connection:
        connection.executescript(
            "CREATE TABLE source_overlays(base TEXT, delta TEXT); INSERT INTO source_overlays VALUES ('old base','new delta');"
        )
    original = storage.storage.index_path(SOURCE_SESSION_ID).read_bytes()
    with pytest.raises(RuntimeError, match="v1_full_copy_not_lossless"):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
            require_lossless=True,
        )
    assert not storage.storage.root(TARGET_SESSION_ID).exists()
    assert storage.storage.index_path(SOURCE_SESSION_ID).read_bytes() == original
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
    validate = migration_importer.validate_staging

    def corrupt(
        staged: RolloutContextStore, target: str, thread: str, namespace: str
    ) -> None:
        queries = {
            "offset": "UPDATE database_meta SET committed_jsonl_offset=0",
            "final": "UPDATE turn_records SET final_item_id=root_input_item_id",
            "view": "DELETE FROM context_view_items",
        }
        with sqlite3.connect(staged.storage.index_path(target)) as connection:
            connection.execute(queries[corruption])
        validate(staged, target, thread, namespace)

    monkeypatch.setattr(migration_importer, "validate_staging", corrupt)
    original = artifact_manifest(storage.storage.root(SOURCE_SESSION_ID))
    with pytest.raises(RuntimeError):
        LegacyRolloutImporter(storage).import_v1(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=storage.storage.main_thread_id(TARGET_SESSION_ID),
            checkpoint_ns="",
        )
    assert not storage.storage.root(TARGET_SESSION_ID).exists()
    assert artifact_manifest(storage.storage.root(SOURCE_SESSION_ID)) == original
    assert migration_audits(storage)[0]["status"] == "failed"
