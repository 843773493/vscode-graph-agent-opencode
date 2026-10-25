"""捕获准备失败保留原错误，已有目录不被当前请求删除。"""
from __future__ import annotations

import errno
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.infrastructure.rollout_context.fork import source_snapshot


@pytest.fixture
def source_capture(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'sentinel').write_bytes(b'original')
    lock = tmp_path / '.rollout.write.lock'
    lock.write_bytes(b'original-lock')
    captured = tmp_path / 'capture'
    storage = SimpleNamespace(storage=SimpleNamespace(root=lambda *_args, **_kwargs: source))

    def capture():
        return source_snapshot.capture_fork_rollout_snapshot(storage, source_session_id='source-session',
            source_thread_id='source-thread', captured_root=captured, snapshot_id='snapshot-test', rollout_lock_held=True)

    return source, lock, captured, capture


@pytest.mark.parametrize('phase', ['source_layout', 'mkdir', 'acl', 'acl_runtime', 'lock_copy'])
def test_capture_preparation_failure_keeps_native_cause_and_source(source_capture, monkeypatch, caplog, phase):
    source, lock, captured, capture = source_capture
    owner, operation, path, stage = {
        'source_layout': (Path, 'is_dir', source, 'source_layout'),
        'mkdir': (Path, 'mkdir', captured, 'preparation'),
        'acl': (source_snapshot, 'restrict_private_path_access', captured, 'preparation'),
        'acl_runtime': (source_snapshot, 'restrict_private_path_access', captured, 'preparation'),
        'lock_copy': (shutil, 'copy2', lock, 'lock_copy'),
    }[phase]
    native = RuntimeError(f'原生 ACL 运行失败 {path}') if phase == 'acl_runtime' else PermissionError(errno.EACCES, f'原生捕获拒绝 {path}', str(path))
    timeout = subprocess.TimeoutExpired(['icacls', str(path)], 10, stderr=str(path).encode())
    real = getattr(owner, operation)

    def fail(value, *args, **kwargs):
        if Path(value) == path:
            if phase == 'acl_runtime':
                raise native from timeout
            raise native
        return real(value, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(owner, operation, fail)
        with pytest.raises(RuntimeError if phase == 'acl_runtime' else PermissionError) as caught:
            capture()
    assert caught.value.__cause__ is native
    assert 'artifact=fork_source stage=' + stage in str(caught.value)
    assert str(source.parent) not in str(caught.value) and str(path) in caplog.text
    if phase == 'acl_runtime':
        assert native.__cause__ is timeout and timeout.timeout == 10
    else:
        assert caught.value.errno == errno.EACCES and caught.value.filename is None
    assert not captured.exists()
    assert (source / 'sentinel').read_bytes() == b'original' and lock.read_bytes() == b'original-lock'


def test_existing_capture_directory_remains_owned_by_original_request(source_capture, caplog):
    source, lock, captured, capture = source_capture
    captured.mkdir()
    sentinel = captured / 'existing-owner'
    sentinel.write_bytes(b'existing snapshot')
    with pytest.raises(FileExistsError) as caught:
        capture()
    assert 'stage=preparation' in str(caught.value) and str(captured) not in str(caught.value)
    assert isinstance(caught.value.__cause__, FileExistsError)
    assert str(captured) in caplog.text and sentinel.read_bytes() == b'existing snapshot'
    assert (source / 'sentinel').read_bytes() == b'original' and lock.read_bytes() == b'original-lock'
