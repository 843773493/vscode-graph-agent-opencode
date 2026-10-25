"""目标暂存和同步失败不得丢失错误归属、跳过文件或删除既有目录。"""

from __future__ import annotations

import errno
import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.infrastructure.rollout_context.fork import snapshot_copy


@pytest.fixture
def staging_fault(monkeypatch):
    """只在真实暂存或同步调用内注入原生失败，不代写产品快照。"""
    errors, paths = [], []

    def fail(path):
        error = PermissionError(errno.EACCES, f"原生暂存拒绝 {path}", str(path))
        errors.append(error)
        paths.append(path)
        raise error

    @contextmanager
    def inject(fault, stage, source=None):
        real_open, real_mkdir = Path.open, Path.mkdir
        real_scan, real_fsync = snapshot_copy.os.scandir, snapshot_copy.os.fsync
        real_rmtree = snapshot_copy.shutil.rmtree
        real_file, real_dir = snapshot_copy.fsync_file, snapshot_copy.fsync_directory
        opened = []

        def open_file(value, mode="r", *args, **kwargs):
            selected = (fault in {"read", "cleanup"} and mode == "rb" and value.is_relative_to(source)
                        or fault in {"write", "flush", "fsync", "close"} and mode == "xb" and value.is_relative_to(stage))
            if not selected or errors:
                return real_open(value, mode, *args, **kwargs)
            if fault in {"read", "cleanup"}:
                fail(value)
            stream = real_open(value, mode, *args, **kwargs)
            opened.append(stream)
            if fault == "fsync":
                return stream
            proxy = Mock(wraps=stream)
            proxy.__enter__ = Mock(return_value=proxy)
            proxy.__exit__ = Mock(side_effect=lambda *_: proxy.close())
            if fault in {"write", "flush"}:
                getattr(proxy, fault).side_effect = lambda *_: fail(value)
            else:
                def close():
                    stream.close()
                    fail(value)
                proxy.close.side_effect = close
            return proxy

        def mkdir(value, *args, **kwargs):
            if value == stage and not errors:
                fail(value)
            return real_mkdir(value, *args, **kwargs)

        def fsync(descriptor):
            if opened and not opened[0].closed and descriptor == opened[0].fileno() and not errors:
                fail(stage)
            return real_fsync(descriptor)

        def cleanup(value, *args, **kwargs):
            if Path(value) == stage and len(errors) == 1:
                fail(stage)
            return real_rmtree(value, *args, **kwargs)

        def scan(value):
            selected = stage / "nested" if fault == "nested_scan" else stage
            if not isinstance(value, int) and Path(value) == selected and not errors:
                fail(selected)
            return real_scan(value)

        def sync(value):
            if value.is_relative_to(stage) and not errors:
                fail(value)
            return (real_file if fault == "file_sync" else real_dir)(value)

        with monkeypatch.context() as patch:
            if fault == "mkdir":
                patch.setattr(Path, "mkdir", mkdir)
            elif fault in {"read", "write", "flush", "fsync", "close", "cleanup"}:
                patch.setattr(Path, "open", open_file)
                if fault == "fsync":
                    patch.setattr(snapshot_copy.os, "fsync", fsync)
                elif fault == "cleanup":
                    patch.setattr(snapshot_copy.shutil, "rmtree", cleanup)
            elif fault in {"scan", "nested_scan"}:
                patch.setattr(snapshot_copy.os, "scandir", scan)
            else:
                patch.setattr(snapshot_copy, "fsync_file" if fault == "file_sync" else "fsync_directory", sync)
            yield
        assert all(stream.closed for stream in opened)

    return SimpleNamespace(inject=inject, errors=errors, paths=paths)


@pytest.fixture
def target_snapshot(tmp_path):
    captured = tmp_path / "captured"
    source = captured / "rollout/nested/rollout.jsonl"
    source.parent.mkdir(parents=True)
    body = b"canonical frozen bytes\n"
    source.write_bytes(body)
    snapshot = SimpleNamespace(rollout=SimpleNamespace(captured_root=captured, files=(SimpleNamespace(
        relative_path="nested/rollout.jsonl", size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest()),)))
    target = tmp_path / "target/rollout"
    target.parent.mkdir()
    stage = target.with_name(".fork-test.rollout")

    def copy():
        return snapshot_copy.stage_captured_rollout(snapshot, target_root=target, materialization_id="test")

    return SimpleNamespace(source=source, body=body, snapshot=snapshot, target=target, stage=stage, copy=copy)


@pytest.mark.parametrize("fault", ["mkdir", "read", "write", "flush", "fsync", "close", "cleanup"])
def test_staging_native_failure_preserves_cause_and_source(target_snapshot, staging_fault, caplog, fault):
    case = target_snapshot
    with staging_fault.inject(fault, case.stage, case.snapshot.rollout.captured_root), pytest.raises(PermissionError) as caught:
        case.copy()
    stage = "staging" if fault == "mkdir" else "staging_cleanup" if fault == "cleanup" else "staging_copy"
    assert "artifact=fork_target stage=" + stage in str(caught.value)
    assert caught.value.errno == errno.EACCES and caught.value.filename is None
    assert caught.value.__cause__ is staging_fault.errors[-1]
    if fault == "cleanup":
        assert staging_fault.errors[-1].__context__.__cause__ is staging_fault.errors[0]
        snapshot_copy.shutil.rmtree(case.stage)
    assert str(case.source.parent.parent.parent) not in str(caught.value)
    assert str(staging_fault.paths[-1]) in caplog.text and not case.stage.exists()
    assert case.source.read_bytes() == case.body
    assert case.copy() == case.stage and (case.stage / "nested/rollout.jsonl").read_bytes() == case.body


def test_existing_staging_directory_is_not_deleted(target_snapshot, caplog):
    case = target_snapshot
    case.stage.mkdir()
    sentinel = case.stage / "existing-owner"
    sentinel.write_bytes(b"other request")
    with pytest.raises(FileExistsError) as caught:
        case.copy()
    assert "artifact=fork_target stage=staging" in str(caught.value)
    assert isinstance(caught.value.__cause__, FileExistsError) and str(case.stage) not in str(caught.value)
    assert sentinel.read_bytes() == b"other request" and case.source.read_bytes() == case.body
    assert str(case.stage) in caplog.text


@pytest.mark.parametrize("fault", ["hash", "source_symlink"])
def test_staging_rejects_invalid_frozen_source_without_exposing_locator(target_snapshot, caplog, fault):
    case = target_snapshot
    if fault == "hash":
        case.source.write_bytes(b"changed capture")
    else:
        case.source.unlink()
        case.source.symlink_to(case.target.parent)
    with pytest.raises(RuntimeError) as caught:
        case.copy()
    assert "artifact=fork_target stage=staging_copy" in str(caught.value)
    assert "nested/rollout.jsonl" not in str(caught.value) and str(case.source) in caplog.text
    assert not case.stage.exists()


@pytest.mark.parametrize("fault", ["scan", "nested_scan", "file_sync", "directory_sync"])
def test_preparation_sync_propagates_native_failure(target_snapshot, staging_fault, caplog, fault):
    case = target_snapshot
    case.copy()
    with staging_fault.inject(fault, case.stage), pytest.raises(PermissionError) as caught:
        snapshot_copy._fsync_tree(case.stage)
    assert "artifact=fork_target stage=preparation_sync" in str(caught.value)
    assert caught.value.__cause__ is staging_fault.errors[0] and caught.value.errno == errno.EACCES
    assert str(case.stage) not in str(caught.value) and str(staging_fault.paths[0]) in caplog.text
    assert (case.stage / "nested/rollout.jsonl").read_bytes() == case.body
    snapshot_copy._fsync_tree(case.stage)


@pytest.mark.parametrize("fault", ["missing", "root_symlink", "directory_symlink", "file_symlink"])
def test_preparation_sync_rejects_missing_or_linked_tree(target_snapshot, caplog, fault):
    case = target_snapshot
    if fault == "root_symlink":
        case.stage.symlink_to(case.source.parent, target_is_directory=True)
    elif fault != "missing":
        case.copy()
        (case.stage / "linked").symlink_to(case.source.parent if fault == "directory_symlink" else case.source,
                                           target_is_directory=fault == "directory_symlink")
    with pytest.raises(RuntimeError) as caught:
        snapshot_copy._fsync_tree(case.stage)
    assert "artifact=fork_target stage=preparation_sync" in str(caught.value)
    assert str(case.stage) not in str(caught.value) and str(case.stage) in caplog.text
    assert case.source.read_bytes() == case.body
