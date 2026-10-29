"""迁移失败审计不持久化原生诊断，发布与恢复仍由原 importer 协调。"""

from __future__ import annotations

import errno
import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from app.services.infrastructure.rollout_context.migration import (
    importer as migration_importer,
)
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
)
from app.services.infrastructure.rollout_context.migration.importer import (
    LegacyRolloutImporter,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from tests.integration.backend.sessions.itemized_migration_helpers import (
    TARGET_SESSION_ID,
    _accepted_records,
    _storage,
    _write_v1_source,
    migration_audits,
    prepare_migration_workspace,
)

SOURCE_SESSION_ID = "ses_019c205391bb7a4b81b6728df70f63e6"


@pytest.fixture
def migration_storage(
    request: pytest.FixtureRequest, session_bundle_factory: Callable
) -> RolloutContextStore:
    sessions = prepare_migration_workspace(request) / ".boxteam" / "sessions"
    _write_v1_source(
        sessions,
        session_id=SOURCE_SESSION_ID,
        session_bundle_factory=session_bundle_factory,
        records=_accepted_records(),
    )
    session_bundle_factory(sessions, TARGET_SESSION_ID)
    return _storage(sessions)


@pytest.fixture
def migration_failure(
    request: pytest.FixtureRequest, migration_storage: RolloutContextStore
) -> BaseException:
    path = str(migration_storage.storage.root(TARGET_SESSION_ID) / "private.json")
    kind = request.param
    if kind == "native":
        return OSError(errno.EACCES, "迁移文件拒绝访问", path)
    if kind == "sqlite":
        error = sqlite3.OperationalError(f"SQLite 访问失败: {path}")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        return error
    if kind == "json":
        # 解码器 message 也可包含调用方补充的原生位置。
        return json.JSONDecodeError(f"无效输入: {path}", "{}\n?", 3)
    if kind == "interrupted":
        return KeyboardInterrupt(f"中断迁移: {path}")
    raise AssertionError(f"未登记的失败场景: {kind}")


@pytest.mark.parametrize("failure_point", ["source", "validation", "installed"])
@pytest.mark.parametrize(
    "migration_failure", ["native", "sqlite", "json", "interrupted"], indirect=True
)
def test_import_failure_audit_preserves_original_error_and_publication(
    migration_storage: RolloutContextStore,
    migration_failure: BaseException,
    failure_point: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = migration_storage
    source = storage.storage.root(SOURCE_SESSION_ID)
    target = storage.storage.root(TARGET_SESSION_ID)
    source_before = artifact_manifest(source)
    source_thread = storage.storage.main_thread_id(SOURCE_SESSION_ID)
    target_thread = storage.storage.main_thread_id(TARGET_SESSION_ID)
    install = migration_importer.install_directory

    def fail(*args: object, **kwargs: object) -> None:
        if failure_point == "installed":
            assert len(args) == 2
            assert isinstance(args[0], Path) and isinstance(args[1], Path)
            install(args[0], args[1])
        else:
            assert not target.exists()
        raise migration_failure

    method = {
        "source": "read_source_report",
        "validation": "validate_staging",
        "installed": "install_directory",
    }[failure_point]
    with monkeypatch.context() as patch:
        patch.setattr(migration_importer, method, fail)
        with (
            caplog.at_level(logging.ERROR, logger="app.core.failures"),
            pytest.raises(type(migration_failure)) as caught,
        ):
            LegacyRolloutImporter(storage).import_v1(
                source_session_id=SOURCE_SESSION_ID,
                source_thread_id=source_thread,
                target_session_id=TARGET_SESSION_ID,
                target_thread_id=target_thread,
                checkpoint_ns="",
            )

    assert caught.value is migration_failure
    audit = migration_audits(storage)[0]
    migration_id = audit["migration_id"]
    assert (
        audit["source_session_id"],
        audit["source_thread_id"],
        audit["target_session_id"],
        audit["target_thread_id"],
        audit["checkpoint_ns"],
    ) == (SOURCE_SESSION_ID, source_thread, TARGET_SESSION_ID, target_thread, "")
    installed = failure_point == "installed"
    assert (audit["status"], audit["rollback"]) == (
        "installed_audit_failed" if installed else "failed",
        "installed_v2_preserved" if installed else "uninstalled_staging_quarantined",
    )
    expected = (
        f"历史迁移失败: stage=legacy-rollout-import:{migration_id}, "
        f"error={type(migration_failure).__name__}"
    )
    if isinstance(migration_failure, OSError):
        expected += f", errno={errno.EACCES}"
    elif isinstance(migration_failure, sqlite3.Error):
        expected += f", sqlite_errorcode={sqlite3.SQLITE_IOERR}"
    elif isinstance(migration_failure, json.JSONDecodeError):
        expected += ", lineno=2, colno=1, pos=3"
    assert audit["error"] == expected
    serialized = json.dumps(audit, ensure_ascii=False)
    assert json.dumps(str(storage.storage.sessions_dir))[1:-1] not in serialized
    assert "private.json" not in serialized
    diagnostic = [
        record
        for record in caplog.records
        if record.exc_info and record.exc_info[1] is migration_failure
    ]
    assert len(diagnostic) == 1
    assert str(migration_failure) in caplog.text
    assert artifact_manifest(source) == source_before

    # 冷 owner 只能验收已发布目标或保留失败审计，不能重新安装 staging。
    fresh = _storage(storage.storage.sessions_dir)
    owner = LegacyRolloutImporter(fresh)
    if installed:
        target_before = artifact_manifest(target)
        recovered = owner.recover(
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=target_thread,
            checkpoint_ns="",
        )
        assert len(recovered) == 1 and recovered[0]["status"] == "installed"
        assert artifact_manifest(target) == target_before
        with fresh.storage.read_connection(
            TARGET_SESSION_ID, target_thread, ""
        ) as connection:
            persisted_report = connection.execute(
                "SELECT report_json FROM legacy_migration_reports WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
        assert persisted_report is not None
        assert json.loads(persisted_report[0]) == audit["result"]
        assert [
            item.payload
            for item in fresh.storage.read_items(TARGET_SESSION_ID, target_thread)
        ] == [
            "迁移前的请求",
            "迁移后的回答",
        ]
    else:
        assert not target.exists()
        assert owner.recover(
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=target_thread,
            checkpoint_ns="",
        ) == []
        assert migration_audits(fresh) == [audit]
        assert not target.exists()
    assert owner.recover(
        target_session_id=TARGET_SESSION_ID,
        target_thread_id=target_thread,
        checkpoint_ns="",
    ) == []
    assert artifact_manifest(source) == source_before
