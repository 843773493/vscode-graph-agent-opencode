"""产品生成历史后，目标暂存/同步失败必须中止发布并允许完整重试。"""

from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import sessions
from app.core.checkpoint_config import build_checkpoint_config
from app.core.session_thread_context import session_thread_owner_scope
from app.core.trace_middleware import TraceMiddleware, get_current_gateway_id
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint import fork_copy, fork_owner
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork import snapshot_copy
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
from tests.unit.services.infrastructure.rollout_context.test_target_publication_io import (
    publication_fault,
)
from tests.unit.services.infrastructure.rollout_context.test_target_snapshot_io import (
    staging_fault,
)

__all__ = ["fork_execution_message", "fork_execution_source", "fork_gateway_request_context",
           "production_fork_container", "publication_fault", "staging_fault"]


@pytest.mark.parametrize("fault", ["mkdir", "read", "write", "cleanup", "scan", "file_sync", "directory_symlink",
    "backup_rename", "backup_sync", "install_rename", "install_sync", "restore_rename", "recovery_exists", "finalize_remove"])
async def test_public_fork_target_failure_keeps_history_and_can_retry(
    production_fork_container, fork_execution_source, staging_fault, publication_fault, monkeypatch, caplog, fault,
):
    container, source = production_fork_container, fork_execution_source
    context = _production_fork_context(container)
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy")
    node = _main_thread_node(context, source.session_id)
    canonical = (node / "rollout/rollout.jsonl").read_bytes()
    with session_thread_owner_scope(MutationIntentOwner(source.session_id, source.thread_id), langgraph_thread_id=source.thread_id):
        checkpoint = context.saver.get_tuple(build_checkpoint_config(source.thread_id))
    assert checkpoint is not None
    assemblies = context.saver._storage.assemblies.list_context_assemblies(source.session_id, source.thread_id)
    assert assemblies
    visible = {item.session_id for item in (await container.session_service.list()).items}
    copy, sync = fork_copy.stage_captured_rollout, snapshot_copy._fsync_tree
    stages = []

    def stage_capture(snapshot, **kwargs):
        stage = kwargs["target_root"].with_name(f'.fork-{kwargs["materialization_id"]}.rollout')
        stages.append(stage)
        with staging_fault.inject(fault, stage, snapshot.rollout.captured_root):
            return copy(snapshot, **kwargs)

    def sync_stage(stage):
        stages.append(stage)
        if fault == "directory_symlink":
            (stage / "invalid-link").symlink_to(node / "rollout", target_is_directory=True)
            return sync(stage)
        with staging_fault.inject(fault, stage):
            return sync(stage)

    publication_faults = {"backup_rename", "backup_sync", "install_rename", "install_sync", "restore_rename", "recovery_exists", "finalize_remove"}
    publication_phase = ("publication_recovery" if fault == "recovery_exists" else
                         "publication_finalize" if fault == "finalize_remove" else "publication")
    operation_name = ("recover_target_snapshot_publication" if fault == "recovery_exists" else
                      "finalize_target_snapshot_publication" if fault == "finalize_remove" else "publish_target_snapshot")
    operation_owner = fork_owner if fault == "finalize_remove" else fork_copy
    operation = getattr(operation_owner, operation_name)

    def publication(storage, **kwargs):
        if stages:
            return operation(storage, **kwargs)
        target = storage.root(kwargs["target_session_id"], kwargs["checkpoint_ns"], thread_id=kwargs["target_thread_id"])
        stages.append(target.with_name(f'.fork-{kwargs["materialization_id"]}.rollout'))
        with publication_fault.inject(fault, target, kwargs["materialization_id"]):
            return operation(storage, **kwargs)

    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(sessions.router, prefix="/api/v1")
    app.dependency_overrides[sessions.verify_local_token] = lambda: "test-local-token"
    app.dependency_overrides[sessions.get_session_context_fork_service] = lambda: container.session_context_fork_service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workspace",
                                 headers={"X-BoxTeam-Gateway-Id": get_current_gateway_id()}) as client:
        with monkeypatch.context() as patch:
            if fault in publication_faults:
                patch.setattr(operation_owner, operation_name, publication)
            elif fault in {"scan", "file_sync", "directory_symlink"}:
                patch.setattr(snapshot_copy, "_fsync_tree", sync_stage)
            else:
                patch.setattr(fork_copy, "stage_captured_rollout", stage_capture)
            failed = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        assert failed.status_code == 500, failed.text
        assert len(stages) == 1 and not stages[0].exists()
        stage = (publication_phase if fault in publication_faults else "preparation_sync" if fault in {"scan", "file_sync", "directory_symlink"}
                 else "staging" if fault == "mkdir" else "staging_cleanup" if fault == "cleanup" else "staging_copy")
        assert "artifact=fork_target stage=" + stage in failed.text
        if fault != "directory_symlink":
            assert "PermissionError" in failed.text and "errno=13" in failed.text
            injected = publication_fault if fault in publication_faults else staging_fault
            assert len(injected.errors) == (2 if fault in {"cleanup", "restore_rename"} else 1)
            assert str(injected.paths[-1]) in caplog.text
        else:
            assert str(stages[0] / "invalid-link") in caplog.text
        assert str(container.workspace_root) not in failed.text
        assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
        assert (node / "rollout/rollout.jsonl").read_bytes() == canonical
        assert {item.session_id for item in (await container.session_service.list()).items} == visible
        record = container.session_service.path_resolver.catalog_store.get_creation_record(context.forks._creation_key(request.idempotency_key))
        assert record.state == "aborted" and str(container.workspace_root) not in str(asdict(record))
        retry = request.model_copy(update={"idempotency_key": f"fork-{uuid4().hex}"})
        response = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=retry.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        target = response.json()["data"]
        with RolloutCheckpointSaver(context.saver._storage.storage.sessions_dir) as cold:
            sid, tid = target["session"]["session_id"], target["thread_id"]
            with session_thread_owner_scope(MutationIntentOwner(sid, tid), langgraph_thread_id=tid):
                restored = cold.get_tuple(build_checkpoint_config(tid, checkpoint_id=checkpoint.checkpoint["id"]))
            assert restored is not None and restored.checkpoint == checkpoint.checkpoint
            assert restored.metadata == checkpoint.metadata and restored.pending_writes == checkpoint.pending_writes
            for assembly in assemblies:
                copied = cold._storage.assemblies.get_context_assembly(sid, tid, assembly_id=assembly.assembly_id)
                assert copied.to_dict() == assembly.to_dict() and copied.request_hash == assembly.request_hash
            assert (cold._storage.storage.fork_paths.thread_node(sid, tid) / "rollout/rollout.jsonl").read_bytes() == canonical
        assert (node / "rollout/rollout.jsonl").read_bytes() == canonical
