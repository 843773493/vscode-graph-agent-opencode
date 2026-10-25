"""产品生成来源后，隔离 SQLite worker 失败不得泄漏路径或发布半个副本。"""
from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import sessions
from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_thread_context import session_thread_owner_scope
from app.core.trace_middleware import TraceMiddleware, get_current_gateway_id
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint import fork_source
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork import source_snapshot
from app.services.infrastructure.rollout_context.fork.full_copy import (
    sqlite_snapshot as worker,
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message,
    fork_execution_source,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _main_thread_node,
    _production_fork_context,
    fork_gateway_request_context,
    production_fork_container,
)

__all__ = ['fork_execution_message', 'fork_execution_source', 'fork_gateway_request_context', 'production_fork_container']


CAPTURE_IO_FAULTS = ['source_layout', 'capture_mkdir', 'acl_runtime', 'lock_copy', 'rollout_copy',
                     'sqlite_snapshot', 'jsonl_open', 'control_read', 'manifest_write', 'manifest_fsync']


@pytest.mark.parametrize('fault', ['child_exit', 'spawn', 'private_create', 'private_cleanup', 'combined', 'capture_cleanup', *CAPTURE_IO_FAULTS])
async def test_public_fork_worker_failure_preserves_history_and_retry(
    production_fork_container, fork_execution_source, monkeypatch, caplog, fault,
):
    container, source = production_fork_container, fork_execution_source
    context = _production_fork_context(container)
    request = await _fork_request(context, source.session_id, creation_kind='new_session_main', mode='full_rollout_copy')
    node = _main_thread_node(context, source.session_id)
    canonical = (node / 'rollout/rollout.jsonl').read_bytes()
    with session_thread_owner_scope(MutationIntentOwner(source.session_id, source.thread_id), langgraph_thread_id=source.thread_id):
        checkpoint = context.saver.get_tuple(build_checkpoint_config(source.thread_id))
    assert checkpoint is not None
    assemblies = context.saver._storage.assemblies.list_context_assemblies(source.session_id, source.thread_id)
    assert assemblies
    visible = {item.session_id for item in (await container.session_service.list()).items}
    run = subprocess.run
    failures = []
    private_errors = []
    capture_roots = []
    io_errors, io_paths = [], []
    capture_snapshot = source_snapshot.capture_fork_rollout_snapshot

    def inject_capture(storage, **kwargs):
        root = kwargs['captured_root']
        owner, operation, path = {
            'source_layout': (Path, 'is_dir', node / 'rollout'),
            'capture_mkdir': (Path, 'mkdir', root),
            'acl_runtime': (source_snapshot, 'restrict_private_path_access', root),
            'lock_copy': (shutil, 'copy2', node / '.rollout.write.lock'),
            'rollout_copy': (shutil, 'copytree', node / 'rollout'),
            'sqlite_snapshot': (Path, 'is_file', root / 'rollout/index.sqlite'),
            'jsonl_open': (Path, 'open', root / 'rollout/rollout.jsonl'),
            'control_read': (Path, 'open', root / 'session-control.sqlite'),
            'manifest_write': (os, 'open', root / 'snapshot-manifest.json'),
            'manifest_fsync': (source_snapshot, 'fsync_directory', root),
        }[fault]
        real = getattr(owner, operation)

        def denied(value, *args, **kw):
            selected = Path(value) == path and not io_errors
            if fault in {'jsonl_open', 'control_read'}:
                selected = selected and bool(args) and args[0] == ('r+b' if fault == 'jsonl_open' else 'rb')
            if selected:
                native = RuntimeError(f'原生 ACL 运行拒绝 {path}') if fault == 'acl_runtime' else PermissionError(errno.EACCES, f'原生捕获拒绝 {path}', str(path))
                if fault == 'rollout_copy':
                    copy = shutil.copy2

                    def deny_copy(source, target):
                        if Path(source) == node / 'rollout/rollout.jsonl':
                            io_errors.append(native)
                            io_paths.append(path)
                            raise native
                        return copy(source, target)

                    # 真实 copytree 汇总原生复制失败，不能用假的 Error 回执替代。
                    return real(value, *args, **kw, copy_function=deny_copy)
                io_errors.append(native)
                io_paths.append(path)
                if fault == 'acl_runtime':
                    raise native from subprocess.TimeoutExpired(['icacls', str(path)], 10)
                raise native
            return real(value, *args, **kw)

        with monkeypatch.context() as patch:
            patch.setattr(owner, operation, denied)
            return capture_snapshot(storage, **kwargs)

    def inject(command, **kwargs):
        if command[-1] == worker._COPY_SCRIPT:
            payload = json.loads(kwargs['input'])
            if payload['rollout_lock_held'] and not failures and fault in {'child_exit', 'spawn', 'combined', 'capture_cleanup'}:
                private = Path(payload['private'])
                assert Path(payload['source']) == node / 'rollout'
                failures.append(private)
                capture_roots.append(Path(payload['destination']).parent)
                if fault == 'spawn':
                    raise PermissionError(errno.EACCES, f'原生启动拒绝 {private}', str(private))
                if fault == 'combined':
                    (private / 'index.sqlite').mkdir()
                    return run(command, **kwargs)
                # 删除空私有目录后仍启动真实解释器，由真实 copyfile 产生原生 stderr。
                private.rmdir()
        return run(command, **kwargs)

    # 在 TemporaryDirectory 实际绑定的删除函数处注入故障。
    mkdir, rmtree = os.mkdir, tempfile._rmtree

    def deny_private(path, *args, **kwargs):
        path = Path(path)
        if ('fork-source-captures' in path.parts and path.parent.name == 'rollout'
                and path.name.startswith('.sqlite-copy-') and not private_errors):
            error = PermissionError(errno.EACCES, f'原生私有目录拒绝 {path}', str(path))
            private_errors.append(error)
            if not failures:
                failures.append(path)
            raise error
        return mkdir(path, *args, **kwargs) if fault == 'private_create' else rmtree(path, *args, **kwargs)

    rmdir = os.rmdir

    def deny_capture(path, *args, **kwargs):
        if capture_roots and Path(path) == capture_roots[0] and not private_errors:
            error = PermissionError(errno.EACCES, f'原生来源快照清理拒绝 {path}', str(path))
            private_errors.append(error)
            raise error
        return rmdir(path, *args, **kwargs)

    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(sessions.router, prefix='/api/v1')
    app.dependency_overrides[sessions.verify_local_token] = lambda: 'test-local-token'
    app.dependency_overrides[sessions.get_session_context_fork_service] = lambda: container.session_context_fork_service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://workspace',
                                 headers={'X-BoxTeam-Gateway-Id': get_current_gateway_id()}) as client:
        with monkeypatch.context() as patch:
            patch.setattr(worker.subprocess, 'run', inject)
            if fault in CAPTURE_IO_FAULTS:
                patch.setattr(fork_source, 'capture_fork_rollout_snapshot', inject_capture)
            if fault in {'private_create', 'private_cleanup', 'combined'}:
                patch.setattr(os if fault == 'private_create' else tempfile,
                              'mkdir' if fault == 'private_create' else '_rmtree', deny_private)
            if fault == 'capture_cleanup':
                patch.setattr(os, 'rmdir', deny_capture)
            failed = await client.post(f'/api/v1/sessions/{source.session_id}/fork-context', json=request.model_dump(mode='json'))
        assert failed.status_code == 500, failed.text
        if fault in CAPTURE_IO_FAULTS:
            assert len(io_errors) == 1
            diagnosed = io_paths[0]
        else:
            assert len(failures) == 1 and not failures[0].exists()
            diagnosed = failures[0]
        stage = {'private_create': 'sqlite_private_create', 'private_cleanup': 'sqlite_private_cleanup',
                 'combined': 'sqlite_private_cleanup', 'capture_cleanup': 'cleanup',
                 'source_layout': 'source_layout', 'capture_mkdir': 'preparation', 'acl_runtime': 'preparation',
                 'lock_copy': 'lock_copy', 'rollout_copy': 'rollout_copy', 'sqlite_snapshot': 'sqlite_snapshot',
                 'jsonl_open': 'jsonl_snapshot', 'control_read': 'control_snapshot',
                 'manifest_write': 'manifest_publish', 'manifest_fsync': 'manifest_publish'}.get(fault, 'sqlite_worker')
        assert 'artifact=fork_source stage=' + stage in failed.text
        if fault == 'child_exit':
            assert 'returncode=1' in failed.text and 'error_type=CalledProcessError' in failed.text
            assert 'FileNotFoundError' in caplog.text
        elif fault in {'acl_runtime', 'rollout_copy'}:
            assert 'error_type=' + ('RuntimeError' if fault == 'acl_runtime' else 'Error') in failed.text
        else:
            assert 'PermissionError' in failed.text and 'errno=13' in failed.text
        if fault in {'combined', 'capture_cleanup'}:
            assert ('IsADirectoryError' if fault == 'combined' else 'FileNotFoundError') in caplog.text
            assert 'returncode=1' in caplog.text and private_errors[0].filename in caplog.text
            assert isinstance(private_errors[0].__context__, RuntimeError)
            assert isinstance(private_errors[0].__context__.__cause__, subprocess.CalledProcessError)
        assert str(container.workspace_root) not in failed.text and str(diagnosed) in caplog.text
        assert failed.headers['X-Request-ID'] == failed.json()['request_id']
        assert (node / 'rollout/rollout.jsonl').read_bytes() == canonical
        assert {item.session_id for item in (await container.session_service.list()).items} == visible
        record = container.session_service.path_resolver.catalog_store.get_creation_record(context.forks._creation_key(request.idempotency_key))
        assert record.state == 'aborted' and str(container.workspace_root) not in str(asdict(record))
        retry = request.model_copy(update={'idempotency_key': f'fork-{uuid4().hex}'})
        response = await client.post(f'/api/v1/sessions/{source.session_id}/fork-context', json=retry.model_dump(mode='json'))
        assert response.status_code == 200, response.text
        target = response.json()['data']
        with RolloutCheckpointSaver(context.saver._storage.storage.sessions_dir) as cold:
            sid, tid = target['session']['session_id'], target['thread_id']
            with session_thread_owner_scope(MutationIntentOwner(sid, tid), langgraph_thread_id=tid):
                restored = cold.get_tuple(build_checkpoint_config(tid, checkpoint_id=checkpoint.checkpoint['id']))
            assert restored is not None
            assert restored.checkpoint == checkpoint.checkpoint
            assert restored.metadata == checkpoint.metadata and restored.pending_writes == checkpoint.pending_writes
            for assembly in assemblies:
                copied = cold._storage.assemblies.get_context_assembly(sid, tid, assembly_id=assembly.assembly_id)
                assert copied.to_dict() == assembly.to_dict() and copied.request_hash == assembly.request_hash
            assert (cold._storage.storage.fork_paths.thread_node(sid, tid) / 'rollout/rollout.jsonl').read_bytes() == canonical
        assert (node / 'rollout/rollout.jsonl').read_bytes() == canonical
