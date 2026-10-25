"""冻结 SQL 声明的 detail 复制只公开逻辑错误，保留原文、来源与请求 hash。"""
from __future__ import annotations

import errno
import hashlib
import shutil
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import sessions
from app.core.trace_middleware import TraceMiddleware, get_current_gateway_id
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork import source_snapshot
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message,
    fork_execution_source,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    projection_draft,
    projection_plan,
    projection_saver,
    projection_workspace_root,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _main_thread_node,
    _production_fork_context,
    fork_gateway_request_context,
    production_fork_container,
)

__all__ = [
    'fork_execution_message',
    'fork_execution_source',
    'fork_gateway_request_context',
    'production_fork_container',
    'projection_draft',
    'projection_plan',
    'projection_saver',
    'projection_workspace_root',
]


@pytest.mark.parametrize('phase', ['metadata', 'mkdir', 'copy', 'stat', 'read'])
def test_declared_detail_native_failure_keeps_original_and_can_retry(
    projection_saver, projection_plan, tmp_path, monkeypatch, caplog, phase,
):
    saver, session_id, node = projection_saver
    ref = projection_plan.selection[2].detail_ref
    source = node / detail_relative_path(ref)
    original = source.read_bytes()
    canonical = (node / 'rollout/rollout.jsonl').read_bytes()
    destination = tmp_path / 'snapshot'
    target = destination / detail_relative_path(ref).relative_to('rollout')
    owner, operation, fault = {
        'metadata': (Path, 'is_symlink', source.parent), 'mkdir': (Path, 'mkdir', target.parent),
        'copy': (shutil, 'copy2', source), 'stat': (Path, 'stat', source), 'read': (Path, 'open', source),
    }[phase]
    native = PermissionError(errno.EACCES, f'原生 detail 拒绝 {fault}', str(fault))
    real = getattr(owner, operation)

    def fail(path, *args, **kwargs):
        if Path(path) == fault:
            raise native
        return real(path, *args, **kwargs)

    thread_id = saver._storage.storage.main_thread_id(session_id)
    with saver._storage.storage.read_connection(session_id, thread_id, '') as connection:
        with monkeypatch.context() as patch:
            patch.setattr(owner, operation, fail)
            with pytest.raises(PermissionError) as caught:
                source_snapshot._copy_declared_details(connection, source_rollout=node / 'rollout', snapshot_rollout=destination)
        assert caught.value.__cause__ is native and caught.value.errno == errno.EACCES
        assert caught.value.filename is None and str(node) not in str(caught.value)
        assert 'artifact=fork_source stage=detail_copy' in str(caught.value)
        assert str(fault) in caplog.text
        restored = source_snapshot._copy_declared_details(connection, source_rollout=node / 'rollout', snapshot_rollout=destination)
    selected, = [item for item in restored if item.relative_path == target.relative_to(destination).as_posix()]
    assert selected.sha256 == hashlib.sha256(original).hexdigest() and selected.size_bytes == len(original)
    assert target.read_bytes() == source.read_bytes() == original
    assert (node / 'rollout/rollout.jsonl').read_bytes() == canonical


@pytest.mark.parametrize('damage', ['symlink_parent', 'symlink_leaf', 'parent_file', 'leaf_directory'])
def test_declared_detail_layout_rejection_keeps_native_location_in_diagnostics(
    projection_saver, projection_plan, tmp_path, caplog, damage,
):
    saver, session_id, node = projection_saver
    source = node / detail_relative_path(projection_plan.selection[2].detail_ref)
    original = source.read_bytes()
    damaged = source.parent if damage in {'symlink_parent', 'parent_file'} else source
    parked = damaged.with_name(damaged.name + '.original')
    damaged.rename(parked)
    if damage.startswith('symlink'):
        damaged.symlink_to(parked, target_is_directory=parked.is_dir())
    elif damage == 'parent_file':
        damaged.write_bytes(b'parent sentinel')
    else:
        damaged.mkdir()
    try:
        thread = saver._storage.storage.main_thread_id(session_id)
        with (
            saver._storage.storage.read_connection(session_id, thread, '') as connection,
            pytest.raises(RuntimeError) as caught,
        ):
            source_snapshot._copy_declared_details(connection, source_rollout=node / 'rollout', snapshot_rollout=tmp_path / 'snapshot')
        assert 'artifact=fork_source stage=detail_copy' in str(caught.value)
        assert str(node) not in str(caught.value) and str(damaged) in caplog.text
        body = parked / source.name if damaged == source.parent else parked
        assert body.read_bytes() == original
    finally:
        damaged.rmdir() if damage == 'leaf_directory' else damaged.unlink()
        parked.rename(damaged)


def test_declared_detail_copy_keeps_optional_missing_body_and_declared_order(
    projection_saver, projection_plan, tmp_path,
):
    saver, session_id, node = projection_saver
    missing = node / detail_relative_path(projection_plan.selection[2].detail_ref)
    raw = missing.read_bytes()
    missing.unlink()
    destination = tmp_path / 'snapshot'
    try:
        thread = saver._storage.storage.main_thread_id(session_id)
        with saver._storage.storage.read_connection(session_id, thread, '') as connection:
            declared = [row[0].removeprefix('rollout/') for row in connection.execute(
                'SELECT relative_path FROM context_plan_details ORDER BY detail_ref',
            ) if row[0] != detail_relative_path(projection_plan.selection[2].detail_ref).as_posix()]
            copied = source_snapshot._copy_declared_details(connection, source_rollout=node / 'rollout', snapshot_rollout=destination)
        assert [item.relative_path for item in copied] == declared
        assert not (destination / missing.relative_to(node / 'rollout')).exists()
        for item in copied:
            body = (node / 'rollout' / item.relative_path).read_bytes()
            assert (destination / item.relative_path).read_bytes() == body
            assert item.sha256 == hashlib.sha256(body).hexdigest() and item.size_bytes == len(body)
    finally:
        missing.write_bytes(raw)


@pytest.mark.parametrize('fault', ['copy', 'read', 'symlink_parent'])
async def test_public_fork_detail_failure_preserves_product_history_hash_and_retry(
    production_fork_container, fork_execution_source, monkeypatch, caplog, fault,
):
    container, source = production_fork_container, fork_execution_source
    context = _production_fork_context(container)
    request = await _fork_request(context, source.session_id, creation_kind='new_session_main', mode='full_rollout_copy')
    node = _main_thread_node(context, source.session_id)
    canonical = (node / 'rollout/rollout.jsonl').read_bytes()
    assemblies = context.saver._storage.assemblies.list_context_assemblies(source.session_id, source.thread_id)
    assembly = assemblies[0]
    ref = next(entry.detail_ref for entry in assembly.selection if entry.detail_ref is not None)
    path = node / detail_relative_path(ref)
    original = path.read_bytes()
    detail = context.saver.read_context_plan_detail(source.session_id, source.thread_id, detail_ref=ref, include_sensitive=True)
    visible = {item.session_id for item in (await container.session_service.list()).items}
    native = PermissionError(errno.EACCES, f'原生复制失败 {path}', str(path))
    copy_details = source_snapshot._copy_declared_details
    invoked = []

    def inject(connection, **kwargs):
        invoked.append(True)
        with monkeypatch.context() as patch:
            if fault == 'symlink_parent':
                parked = path.parent.with_name(path.parent.name + '.original')
                path.parent.rename(parked)
                path.parent.symlink_to(parked, target_is_directory=True)
                try:
                    return copy_details(connection, **kwargs)
                finally:
                    path.parent.unlink()
                    parked.rename(path.parent)
            owner, operation = (shutil, 'copy2') if fault == 'copy' else (Path, 'open')
            real = getattr(owner, operation)

            def denied(value, *args, **kw):
                if Path(value) == path:
                    raise native
                return real(value, *args, **kw)

            patch.setattr(owner, operation, denied)
            return copy_details(connection, **kwargs)

    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(sessions.router, prefix='/api/v1')
    app.dependency_overrides[sessions.verify_local_token] = lambda: 'test-local-token'
    app.dependency_overrides[sessions.get_session_context_fork_service] = lambda: container.session_context_fork_service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://workspace',
                                 headers={'X-BoxTeam-Gateway-Id': get_current_gateway_id()}) as client:
        with monkeypatch.context() as patch:
            patch.setattr(source_snapshot, '_copy_declared_details', inject)
            failed = await client.post(f'/api/v1/sessions/{source.session_id}/fork-context', json=request.model_dump(mode='json'))
        assert failed.status_code == 500, failed.text
        assert invoked and 'artifact=fork_source stage=detail_copy' in failed.text
        assert str(container.workspace_root) not in failed.text
        diagnosed_path = path.parent if fault == 'symlink_parent' else path
        assert str(diagnosed_path) in caplog.text
        assert failed.headers['X-Request-ID'] == failed.json()['request_id']
        assert path.read_bytes() == original and (node / 'rollout/rollout.jsonl').read_bytes() == canonical
        assert {item.session_id for item in (await container.session_service.list()).items} == visible
        record = container.session_service.path_resolver.catalog_store.get_creation_record(context.forks._creation_key(request.idempotency_key))
        assert record.state == 'aborted' and str(container.workspace_root) not in str(asdict(record))
        retry = request.model_copy(update={'idempotency_key': f'fork-{uuid4().hex}'})
        response = await client.post(f'/api/v1/sessions/{source.session_id}/fork-context', json=retry.model_dump(mode='json'))
        assert response.status_code == 200, response.text
        target = response.json()['data']
        with RolloutCheckpointSaver(context.saver._storage.storage.sessions_dir) as cold:
            sid, tid = target['session']['session_id'], target['thread_id']
            assert cold.read_context_plan_detail(sid, tid, detail_ref=ref, include_sensitive=True) == detail
            restored = cold._storage.assemblies.get_context_assembly(sid, tid, assembly_id=assembly.assembly_id)
            assert restored.request_hash == assembly.request_hash
            assert (cold._storage.storage.fork_paths.thread_node(sid, tid) / 'rollout/rollout.jsonl').read_bytes() == canonical
        assert path.read_bytes() == original
