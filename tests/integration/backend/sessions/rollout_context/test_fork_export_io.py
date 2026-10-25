"""真实消息历史跨 owner fork 时，冻结文件传输失败须明确中止且可重试。"""

from __future__ import annotations

import errno
import os
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import sessions
from app.core.checkpoint_config import build_checkpoint_config
from app.core.resource_uri import resource_display_uri, thread_resource_uri
from app.core.session_thread_context import session_thread_owner_scope
from app.core.trace_middleware import TraceMiddleware
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork import transfer
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
    remote_fork_target,
)

__all__ = ["fork_execution_message", "fork_execution_source", "fork_gateway_request_context",
           "production_fork_container", "remote_fork_target"]

SOURCE_GATEWAY = "gateway_4aae160f813ed87bcf65a8bb8bfd3729"
TARGET_GATEWAY = "gateway_2d2ab24c1f08e28f5f0217140c8be744"


@pytest.mark.parametrize("fault", ["root_scan", "nested_scan", "metadata", "chunk_open", "directory_symlink",
    "receive_path", "receive_mkdir", "receive_open", "receive_write", "receive_fsync", "receive_close",
    "receive_publish", "receive_root_publish"])
async def test_public_remote_fork_transfer_failure_keeps_history_and_can_retry(
    production_fork_container, fork_execution_source, remote_fork_target,
    monkeypatch, caplog, tmp_path, fault,
) -> None:
    source_container, source = production_fork_container, fork_execution_source
    target, transport = remote_fork_target
    context = _production_fork_context(source_container)
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy")
    request = request.model_copy(update={
        "source_uri": thread_resource_uri(source.workspace_id, source.session_id, source.thread_id,
                                          gateway_authority=SOURCE_GATEWAY),
        "target_container_uri": resource_display_uri(scope="workspace", scope_id=target.workspace_id,
            kind="session", tail_segments=(), gateway_authority=TARGET_GATEWAY),
    })
    canonical = _main_thread_node(context, source.session_id) / "rollout/rollout.jsonl"
    original = canonical.read_bytes()
    with session_thread_owner_scope(MutationIntentOwner(source.session_id, source.thread_id), langgraph_thread_id=source.thread_id):
        checkpoint = context.saver.get_tuple(build_checkpoint_config(source.thread_id))
    assert checkpoint is not None
    assemblies = context.saver._storage.assemblies.list_context_assemblies(source.session_id, source.thread_id)
    assert assemblies
    visible = {item.session_id for item in (await target.session_service.list()).items}
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside untouched")
    dispatch = transport.fork_transfer
    failures, paths = [], []
    armed = False

    async def inject(payload):
        nonlocal armed
        action = "chunk" if fault == "chunk_open" else "files"
        if payload.action != action or armed:
            return await dispatch(payload)
        armed = True
        record = payload.capture.to_record()
        root = transfer.capture_directory(context.saver._storage.storage.sessions_dir, record)
        if fault == "directory_symlink":
            path = root / source.session_id / "rollout/link"
            path.symlink_to(outside, target_is_directory=True)
            paths.append(path)
            return await dispatch(payload)
        path = (root if fault == "root_scan" else root / source.session_id / "rollout" if fault == "nested_scan"
                else root / payload.file_name if fault == "chunk_open" else root / source.session_id / "rollout/rollout.jsonl")
        owner, operation = (os, "scandir") if fault.endswith("scan") else (Path, "open" if fault == "chunk_open" else "stat")
        real = getattr(owner, operation)

        def denied(value, *args, **kwargs):
            if not isinstance(value, int) and Path(value) == path and not failures:
                native = PermissionError(errno.EACCES, f"原生导出拒绝 {path}", str(path))
                failures.append(native)
                paths.append(path)
                raise native
            return real(value, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(owner, operation, denied)
            return await dispatch(payload)

    receive = transfer.receive_transfer_file
    artifact_file = transfer._artifact_file
    sync_directory = transfer.fsync_directory
    root_syncs = 0

    def fail_native(path):
        native = PermissionError(errno.EACCES, f"原生接收拒绝 {path}", str(path))
        failures.append(native)
        paths.append(path)
        raise native

    def receive_path(root, record, name):
        if fault == "receive_path" and root.is_relative_to(target.workspace_root) and not failures:
            real = Path.is_symlink

            def denied(value):
                if value == root:
                    fail_native(root)
                return real(value)

            with monkeypatch.context() as patch:
                patch.setattr(Path, "is_symlink", denied)
                return artifact_file(root, record, name)
        return artifact_file(root, record, name)

    def receive_root_publish(path):
        nonlocal root_syncs
        if (path.is_relative_to(target.workspace_root) and path.parent.name == "fork-source-captures"
                and not failures):
            root_syncs += 1
            # 第一次是 marker 文件发布；第二次才是整份 artifact 的目录发布。
            if root_syncs == 2:
                fail_native(path)
        return sync_directory(path)

    async def inject_receive(*args, **kwargs):
        nonlocal armed
        if armed or (fault in {"receive_write", "receive_fsync", "receive_close"} and not kwargs["size_bytes"]):
            return await receive(*args, **kwargs)
        armed = True
        path = kwargs["path"]
        real_open, real_mkdir, real_fsync = Path.open, Path.mkdir, os.fsync
        opened = []

        def denied_open(value, *open_args, **open_kwargs):
            if value != path:
                return real_open(value, *open_args, **open_kwargs)
            if fault == "receive_open":
                fail_native(path)
            stream = real_open(value, *open_args, **open_kwargs)
            opened.append(stream)
            if fault not in {"receive_write", "receive_close"}:
                return stream
            writer = Mock(wraps=stream)
            writer.__enter__ = Mock(return_value=writer)
            writer.__exit__ = Mock(side_effect=lambda *_: writer.close())
            if fault == "receive_write":
                writer.write.side_effect = lambda *_: fail_native(path)
            else:
                def denied_close():
                    stream.close()
                    fail_native(path)
                writer.close.side_effect = denied_close
            return writer

        def denied_mkdir(value, *mkdir_args, **mkdir_kwargs):
            if value == path.parent and not failures:
                fail_native(value)
            return real_mkdir(value, *mkdir_args, **mkdir_kwargs)

        def denied_fsync(descriptor):
            if opened and not opened[0].closed and descriptor == opened[0].fileno() and not failures:
                fail_native(path)
            return real_fsync(descriptor)

        def denied_publish(value):
            if value == path.parent and not failures:
                fail_native(value)
            return sync_directory(value)

        with monkeypatch.context() as patch:
            if fault == "receive_mkdir":
                patch.setattr(Path, "mkdir", denied_mkdir)
            else:
                patch.setattr(Path, "open", denied_open)
            if fault == "receive_fsync":
                patch.setattr(os, "fsync", denied_fsync)
            if fault == "receive_publish":
                patch.setattr(transfer, "fsync_directory", denied_publish)
            return await receive(*args, **kwargs)

    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(sessions.router, prefix="/api/v1")
    app.dependency_overrides[sessions.verify_local_token] = lambda: "test-local-token"
    app.dependency_overrides[sessions.get_session_context_fork_service] = lambda: target.session_context_fork_service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workspace",
                                 headers={"X-BoxTeam-Gateway-Id": TARGET_GATEWAY}) as client:
        with monkeypatch.context() as patch:
            if fault == "receive_path":
                patch.setattr(transfer, "_artifact_file", receive_path)
            elif fault == "receive_root_publish":
                patch.setattr(transfer, "fsync_directory", receive_root_publish)
            elif fault.startswith("receive_"):
                patch.setattr(transfer, "receive_transfer_file", inject_receive)
            else:
                patch.setattr(transport, "fork_transfer", inject)
            failed = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        assert failed.status_code == 500, failed.text
        assert len(paths) == 1
        if fault == "receive_root_publish":
            assert root_syncs == 2
        stage = {"receive_path": "receive_path", "receive_mkdir": "receive_open", "receive_open": "receive_open",
                 "receive_write": "receive_write", "receive_fsync": "receive_flush", "receive_close": "receive_close",
                 "receive_publish": "receive_publish", "receive_root_publish": "receive_publish"}.get(
                     fault, "export_chunk" if fault == "chunk_open" else "export_manifest")
        assert "artifact=fork_transfer stage=" + stage in failed.text
        if fault == "directory_symlink":
            assert "符号链接目录" in failed.text
        else:
            assert len(failures) == 1 and "PermissionError" in failed.text and "errno=13" in failed.text
        assert str(paths[0]) in caplog.text
        assert all(str(container.workspace_root) not in failed.text for container in (source_container, target))
        assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
        assert canonical.read_bytes() == original and sentinel.read_bytes() == b"outside untouched"
        assert {item.session_id for item in (await target.session_service.list()).items} == visible
        key = target.session_context_fork_service._creation_key(request.idempotency_key)
        for container in (source_container, target):
            capture = container.session_service.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
            assert capture is not None and capture.state == "aborted"
            assert str(source_container.workspace_root) not in str(asdict(capture))
            assert str(target.workspace_root) not in str(asdict(capture))
        creation = target.session_service.path_resolver.catalog_store.get_creation_record(key)
        assert creation.state == "aborted"
        retry = request.model_copy(update={"idempotency_key": "fork-" + uuid4().hex})
        response = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=retry.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        sid, tid = result["session"]["session_id"], result["thread_id"]
        with RolloutCheckpointSaver(target.checkpointer._storage.storage.sessions_dir) as cold:
            with session_thread_owner_scope(MutationIntentOwner(sid, tid), langgraph_thread_id=tid):
                restored = cold.get_tuple(build_checkpoint_config(tid, checkpoint_id=checkpoint.checkpoint["id"]))
            assert restored is not None and restored.checkpoint == checkpoint.checkpoint
            assert restored.metadata == checkpoint.metadata and restored.pending_writes == checkpoint.pending_writes
            for assembly in assemblies:
                copied = cold._storage.assemblies.get_context_assembly(sid, tid, assembly_id=assembly.assembly_id)
                assert copied.to_dict() == assembly.to_dict() and copied.request_hash == assembly.request_hash
            assert (cold._storage.storage.fork_paths.thread_node(sid, tid) / "rollout/rollout.jsonl").read_bytes() == original
        assert canonical.read_bytes() == original and sentinel.read_bytes() == b"outside untouched"
