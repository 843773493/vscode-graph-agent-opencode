"""来源快照清理失败必须暴露，且保留此前的捕获错误。"""
from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.infrastructure.rollout_context.fork import source_snapshot


@pytest.mark.parametrize('primary', ['missing_lock', 'acl'])
def test_fork_snapshot_reports_native_cleanup_failure_with_original_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, primary: str,
) -> None:
    source = tmp_path / 'source-rollout'
    source.mkdir()
    original = b'original source sentinel'
    (source / 'sentinel').write_bytes(original)
    captured = tmp_path / 'capture'
    storage = SimpleNamespace(storage=SimpleNamespace(root=lambda *_args, **_kwargs: source))
    native = PermissionError(errno.EACCES, f'原生快照清理拒绝 {captured}', str(captured))
    acl = PermissionError(errno.EACCES, f'原生 ACL 拒绝 {captured}', str(captured))
    rmdir = os.rmdir
    denied = []

    def deny_rmdir(path, *args, **kwargs):
        if Path(path) == captured:
            denied.append(native)
            raise native
        return rmdir(path, *args, **kwargs)

    def reject_acl(*args, **kwargs):
        raise acl

    with monkeypatch.context() as patch:
        patch.setattr(os, 'rmdir', deny_rmdir)
        if primary == 'acl':
            patch.setattr(source_snapshot, 'restrict_private_path_access', reject_acl)
        with pytest.raises(PermissionError) as caught:
            source_snapshot.capture_fork_rollout_snapshot(storage, source_session_id='source-session',
                source_thread_id='source-thread', captured_root=captured, snapshot_id='snapshot-test', rollout_lock_held=True)
    assert denied == [native] and caught.value.__cause__ is native
    assert caught.value.errno == errno.EACCES and caught.value.filename is None
    assert 'artifact=fork_source stage=cleanup' in str(caught.value) and str(captured) not in str(caught.value)
    assert str(captured) in caplog.text and captured.is_dir()
    if primary == 'acl':
        assert isinstance(native.__context__, PermissionError)
        assert native.__context__.__cause__ is acl
        assert native.__context__.errno == errno.EACCES and native.__context__.filename is None
        assert 'stage=preparation' in str(native.__context__)
    else:
        assert isinstance(native.__context__, RuntimeError) and '缺少既有普通写锁文件' in str(native.__context__)
    assert (source / 'sentinel').read_bytes() == original
    shutil.rmtree(captured)
    with pytest.raises(RuntimeError, match='缺少既有普通写锁文件'):
        source_snapshot.capture_fork_rollout_snapshot(storage, source_session_id='source-session',
            source_thread_id='source-thread', captured_root=captured, snapshot_id='snapshot-test', rollout_lock_held=True)
    assert not captured.exists() and (source / 'sentinel').read_bytes() == original
