"""SQLite 独立进程失败保留诊断与原调用语义，成功复制仍使用固定读事务。"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import psutil
import pytest

from app.core.sqlite_state import sqlite_file_uri
from app.services.infrastructure.rollout_context.fork.full_copy import (
    sqlite_snapshot as worker,
)


@dataclass(frozen=True)
class DatabaseFiles:
    source: Path
    destination: Path
    connection: sqlite3.Connection

    def source_bytes(self):
        return {path.name: path.read_bytes() for path in self.source.iterdir() if path.is_file()}


@pytest.fixture
def databases(tmp_path):
    source, destination = tmp_path / 'source', tmp_path / 'destination'
    source.mkdir()
    destination.mkdir()
    with closing(sqlite3.connect(sqlite_file_uri(source / 'index.sqlite'), uri=True)) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE history (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        connection.execute("INSERT INTO history VALUES (1,'冻结正文')")
        connection.commit()
        yield DatabaseFiles(source, destination, connection)


@pytest.mark.parametrize('held', [False, True])
def test_real_worker_preserves_committed_bytes_and_rows(databases, held):
    before = databases.source_bytes()
    rows = worker.copy_live_sqlite_snapshot(databases.source, databases.destination, rollout_lock_held=held)
    name, size, digest = rows[0]
    raw = (databases.destination / name).read_bytes()
    assert name == 'index.sqlite' and size == len(raw) and digest == hashlib.sha256(raw).hexdigest()
    with closing(sqlite3.connect(sqlite_file_uri(databases.destination / name) + '?mode=ro&immutable=1', uri=True)) as cold:
        assert cold.execute('SELECT * FROM history').fetchall() == [(1, '冻结正文')]
    after = databases.source_bytes()
    assert after.keys() == before.keys()
    # session-control 的在线 reader 会更新 SHM 读标记；持 rollout 锁时连 SHM 也不能改变。
    assert {name: raw for name, raw in after.items() if held or name != 'index.sqlite-shm'} == {
        name: raw for name, raw in before.items() if held or name != 'index.sqlite-shm'
    }
    assert databases.connection.execute('SELECT * FROM history').fetchall() == [(1, '冻结正文')]
    assert not list(databases.destination.glob('.sqlite-copy-*'))


def test_real_worker_failure_keeps_stderr_exit_and_source_and_retry(databases, monkeypatch, caplog):
    before = databases.source_bytes()
    run = subprocess.run
    missing = []

    def remove_private(command, **kwargs):
        request = json.loads(kwargs['input'])
        path = Path(request['private'])
        path.rmdir()
        missing.append(path)
        return run(command, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(worker.subprocess, 'run', remove_private)
        with pytest.raises(RuntimeError) as caught:
            worker.copy_live_sqlite_snapshot(databases.source, databases.destination, rollout_lock_held=True)
    assert str(databases.source) not in str(caught.value) and str(databases.destination) not in str(caught.value)
    assert 'artifact=fork_source stage=sqlite_worker' in str(caught.value) and 'returncode=1' in str(caught.value)
    cause = caught.value.__cause__
    assert isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 1
    assert b'FileNotFoundError' in cause.stderr and str(missing[0]).encode() in cause.stderr
    assert str(missing[0]) in caplog.text and 'FileNotFoundError' in caplog.text
    assert databases.source_bytes() == before
    worker.copy_live_sqlite_snapshot(databases.source, databases.destination, rollout_lock_held=True)
    assert databases.source_bytes() == before and not list(databases.destination.glob('.sqlite-copy-*'))


def test_native_spawn_failure_keeps_errno_cause_and_private_cleanup(databases, monkeypatch, caplog):
    before = databases.source_bytes()
    error = PermissionError(errno.EACCES, f'原生启动拒绝 {databases.source}', str(databases.source))

    def fail(*args, **kwargs):
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(worker.subprocess, 'run', fail)
        with pytest.raises(PermissionError) as caught:
            worker.copy_live_sqlite_snapshot(databases.source, databases.destination)
    assert caught.value.__cause__ is error and caught.value.errno == errno.EACCES
    assert caught.value.filename is None and str(databases.source) not in str(caught.value)
    assert 'artifact=fork_source stage=sqlite_worker' in str(caught.value)
    assert str(databases.source) in caplog.text
    assert databases.source_bytes() == before and not list(databases.destination.glob('.sqlite-copy-*'))


def test_timeout_keeps_original_budget_and_diagnostics(databases, monkeypatch, caplog):
    before = databases.source_bytes()
    native = subprocess.TimeoutExpired([str(databases.source / 'private-python')], 30,
                                       output=b'partial-output', stderr=str(databases.source).encode())

    def fail(*args, **kwargs):
        assert kwargs['timeout'] == 30
        raise native

    with monkeypatch.context() as patch:
        patch.setattr(worker.subprocess, 'run', fail)
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            worker.copy_live_sqlite_snapshot(databases.source, databases.destination)
    assert caught.value.__cause__ is native and caught.value.timeout == 30
    assert str(databases.source) not in str(caught.value) and 'stage=sqlite_timeout' in str(caught.value)
    assert str(databases.source) in caplog.text and native.output == b'partial-output'
    assert databases.source_bytes() == before and not list(databases.destination.glob('.sqlite-copy-*'))


def test_real_timeout_reaps_worker_before_private_cleanup(databases, monkeypatch, caplog):
    before = databases.source_bytes()
    script = '''import json,os,sys,time
from pathlib import Path
request=json.loads(sys.stdin.buffer.read())
path=Path(request['destination'])
(path/'timed-worker.pid').write_text(str(os.getpid()))
print(str(path),file=sys.stderr,flush=True)
time.sleep(60)
'''
    monkeypatch.setattr(worker, '_COPY_SCRIPT', script)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        worker.copy_live_sqlite_snapshot(databases.source, databases.destination)
    pid = int((databases.destination / 'timed-worker.pid').read_text())
    assert caught.value.timeout == 30 and not psutil.pid_exists(pid)
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)
    assert 'stage=sqlite_timeout' in str(caught.value) and str(databases.destination) not in str(caught.value)
    assert str(databases.destination) in caplog.text
    assert databases.source_bytes() == before and not list(databases.destination.glob('.sqlite-copy-*'))


@pytest.mark.parametrize('fault', ['create', 'cleanup', 'combined'])
def test_private_directory_failure_keeps_native_cause_and_retry(databases, monkeypatch, caplog, fault):
    before = databases.source_bytes()
    # 在 TemporaryDirectory 实际绑定的删除函数处注入故障。
    mkdir, rmtree, run = os.mkdir, tempfile._rmtree, subprocess.run
    errors, private_dirs = [], []

    def denied(path, *args, **kwargs):
        path = Path(path)
        if path.name.startswith('.sqlite-copy-'):
            error = PermissionError(errno.EACCES, f'原生私有目录拒绝 {path}', str(path))
            errors.append(error)
            private_dirs.append(path)
            raise error
        return mkdir(path, *args, **kwargs) if fault == 'create' else rmtree(path, *args, **kwargs)

    def block_copy(command, **kwargs):
        private = Path(json.loads(kwargs['input'])['private'])
        (private / 'index.sqlite').mkdir()
        return run(command, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os if fault == 'create' else tempfile, 'mkdir' if fault == 'create' else '_rmtree', denied)
        if fault == 'combined':
            patch.setattr(worker.subprocess, 'run', block_copy)
        with pytest.raises(PermissionError) as caught:
            worker.copy_live_sqlite_snapshot(databases.source, databases.destination, rollout_lock_held=True)
    assert len(errors) == 1 and caught.value.__cause__ is errors[0]
    assert caught.value.errno == errno.EACCES and caught.value.filename is None
    stage = 'sqlite_private_create' if fault == 'create' else 'sqlite_private_cleanup'
    assert 'artifact=fork_source stage=' + stage in str(caught.value)
    assert str(databases.destination) not in str(caught.value) and str(private_dirs[0]) in caplog.text
    if fault == 'combined':
        failure = errors[0].__context__
        assert isinstance(failure, RuntimeError) and 'stage=sqlite_worker' in str(failure)
        assert isinstance(failure.__cause__, subprocess.CalledProcessError)
        assert failure.__cause__.returncode == 1 and b'IsADirectoryError' in failure.__cause__.stderr
        assert 'IsADirectoryError' in caplog.text and 'returncode=1' in caplog.text
    if fault != 'create':
        assert private_dirs[0].is_dir()
        rmtree(private_dirs[0])
    assert databases.source_bytes() == before
    restored = worker.copy_live_sqlite_snapshot(databases.source, databases.destination, rollout_lock_held=True)
    assert restored and not list(databases.destination.glob('.sqlite-copy-*'))
    assert databases.source_bytes() == before
