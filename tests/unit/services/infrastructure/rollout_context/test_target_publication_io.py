"""发布、回滚及恢复原生失败保留原因与文件事实，普通错误不输出位置。"""

from __future__ import annotations

import errno
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.infrastructure.rollout_context.fork import snapshot_copy


@pytest.fixture
def publication_fault(monkeypatch):
    errors, paths = [], []

    def fail(path):
        error = PermissionError(errno.EACCES, f"原生发布拒绝 {path}", str(path))
        errors.append(error)
        paths.append(path)
        raise error

    @contextmanager
    def inject(fault, target, materialization_id):
        stage = target.with_name(f".fork-{materialization_id}.rollout")
        rollback = target.with_name(f".fork-{materialization_id}.rollback")
        rename, exists = Path.rename, Path.exists
        remove, sync = snapshot_copy.shutil.rmtree, snapshot_copy.fsync_directory
        syncs = 0

        def move(value, destination):
            selected = (
                fault == "backup_rename" and value == target
                or fault in {"install_rename", "restore_rename"} and value == stage
                or fault in {"restore_rename", "recovery_rename"} and value == rollback
            )
            if selected:
                fail(value)
            return rename(value, destination)

        def present(value):
            if fault in {"recovery_exists", "finalize_exists"} and value == rollback and not errors:
                fail(value)
            return exists(value)

        def cleanup(value, *args, **kwargs):
            selected = stage if fault == "recovery_remove" else rollback
            if fault in {"recovery_remove", "finalize_remove"} and Path(value) == selected and not errors:
                fail(value)
            return remove(value, *args, **kwargs)

        def durable(value):
            nonlocal syncs
            if value == target.parent:
                syncs += 1
                selected = 2 if fault == "install_sync" else 1
                if fault in {"backup_sync", "install_sync", "recovery_sync", "finalize_sync"} and syncs == selected:
                    fail(value)
            return sync(value)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "rename", move)
            patch.setattr(Path, "exists", present)
            patch.setattr(snapshot_copy.shutil, "rmtree", cleanup)
            patch.setattr(snapshot_copy, "fsync_directory", durable)
            yield

    return SimpleNamespace(inject=inject, errors=errors, paths=paths)


@pytest.fixture
def publication_case(tmp_path):
    target = tmp_path / "target/rollout"
    target.mkdir(parents=True)
    (target / "original").write_bytes(b"old empty target")
    stage = target.with_name(".fork-test.rollout")
    rollback = target.with_name(".fork-test.rollback")
    storage = SimpleNamespace(root=lambda *_args, **_kwargs: target,
                              _lock=lambda *_args, **_kwargs: nullcontext())
    options = {"target_session_id": "target", "target_thread_id": "thread", "checkpoint_ns": "", "materialization_id": "test"}

    def prepare():
        stage.mkdir()
        (stage / "original").write_bytes(b"frozen history")

    prepare()
    return SimpleNamespace(target=target, stage=stage, rollback=rollback, storage=storage,
                           options=options, prepare=prepare)


def _assert_error(caught, fault, case, caplog, phase):
    assert f"artifact=fork_target stage={phase}" in str(caught.value)
    assert caught.value.errno == errno.EACCES and caught.value.filename is None
    assert caught.value.__cause__ is fault.errors[-1]
    assert str(case.target.parent) not in str(caught.value)
    assert str(fault.paths[-1]) in caplog.text


@pytest.mark.parametrize("fault", ["backup_rename", "backup_sync", "install_rename", "install_sync", "restore_rename"])
def test_publication_failure_preserves_original_and_uses_same_recovery(
    publication_case, publication_fault, caplog, fault,
):
    case = publication_case
    with publication_fault.inject(fault, case.target, "test"), pytest.raises(PermissionError) as caught:
        snapshot_copy.publish_target_snapshot(case.storage, stage=case.stage, **case.options)
    _assert_error(caught, publication_fault, case, caplog, "publication")
    if fault == "restore_rename":
        assert publication_fault.errors[-1].__context__ is publication_fault.errors[0]
    original = case.target if case.target.exists() else case.rollback
    assert (original / "original").read_bytes() == b"old empty target"
    snapshot_copy.recover_target_snapshot_publication(case.storage, **case.options)
    assert (case.target / "original").read_bytes() == b"old empty target"
    assert not case.stage.exists() and not case.rollback.exists()
    case.prepare()
    snapshot_copy.publish_target_snapshot(case.storage, stage=case.stage, **case.options)
    assert (case.target / "original").read_bytes() == b"frozen history"
    assert (case.rollback / "original").read_bytes() == b"old empty target"
    snapshot_copy.finalize_target_snapshot_publication(case.storage, **case.options)
    assert not case.rollback.exists()


@pytest.mark.parametrize("fault", ["recovery_exists", "recovery_rename", "recovery_remove", "recovery_sync"])
def test_recovery_native_failure_preserves_cause_and_original(
    publication_case, publication_fault, caplog, fault,
):
    case = publication_case
    case.target.rename(case.rollback)
    with publication_fault.inject(fault, case.target, "test"), pytest.raises(PermissionError) as caught:
        snapshot_copy.recover_target_snapshot_publication(case.storage, **case.options)
    _assert_error(caught, publication_fault, case, caplog, "publication_recovery")
    original = case.target if case.target.exists() else case.rollback
    assert (original / "original").read_bytes() == b"old empty target"
    snapshot_copy.recover_target_snapshot_publication(case.storage, **case.options)
    assert (case.target / "original").read_bytes() == b"old empty target"
    assert not case.rollback.exists() and not case.stage.exists()


@pytest.mark.parametrize("fault", ["finalize_exists", "finalize_remove", "finalize_sync"])
def test_finalize_failure_does_not_rewrite_installed_history(
    publication_case, publication_fault, caplog, fault,
):
    case = publication_case
    snapshot_copy.publish_target_snapshot(case.storage, stage=case.stage, **case.options)
    with publication_fault.inject(fault, case.target, "test"), pytest.raises(PermissionError) as caught:
        snapshot_copy.finalize_target_snapshot_publication(case.storage, **case.options)
    _assert_error(caught, publication_fault, case, caplog, "publication_finalize")
    assert (case.target / "original").read_bytes() == b"frozen history"
    snapshot_copy.finalize_target_snapshot_publication(case.storage, **case.options)
    assert not case.rollback.exists() and (case.target / "original").read_bytes() == b"frozen history"
