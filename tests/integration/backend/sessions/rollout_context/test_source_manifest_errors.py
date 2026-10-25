"""产品执行生成 fork 来源后，清单读取失败不得泄漏路径或发布残缺副本。"""

from __future__ import annotations

import errno
import os
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import sessions
from app.container import AppContainer
from app.core.trace_middleware import TraceMiddleware, get_current_gateway_id
from app.schemas.internal_v2.session import SessionDTO
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
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

__all__ = ["fork_execution_message", "fork_execution_source", "fork_gateway_request_context", "production_fork_container"]


@pytest.mark.parametrize("fault", ["scan", "symlink"])
async def test_public_fork_source_manifest_failure_keeps_original_and_can_retry(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    fault: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    container = production_fork_container
    source = fork_execution_source
    context = _production_fork_context(container)
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy")
    source_rollout = _main_thread_node(context, source.session_id) / "rollout"
    canonical = source_rollout / "rollout.jsonl"
    original = canonical.read_bytes()
    original_sessions = {item.session_id for item in (await container.session_service.list()).items}
    link = source_rollout / "private-manifest-link"
    scandir = os.scandir
    native = PermissionError(errno.EACCES, f"不能扫描 {source_rollout}", str(source_rollout))

    def denied(path):
        if not isinstance(path, int) and Path(path) == source_rollout:
            raise native
        return scandir(path)

    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(sessions.router, prefix="/api/v1")
    app.dependency_overrides[sessions.verify_local_token] = lambda: "test-local-token"
    app.dependency_overrides[sessions.get_session_context_fork_service] = lambda: container.session_context_fork_service
    headers = {"X-BoxTeam-Gateway-Id": get_current_gateway_id()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workspace", headers=headers) as client:
        try:
            with monkeypatch.context() as patch:
                if fault == "scan":
                    patch.setattr(os, "scandir", denied)
                else:
                    link.symlink_to(canonical)
                failed = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        finally:
            if fault == "symlink":
                link.unlink()
        assert failed.status_code == 500, failed.text
        assert "artifact=fork_source stage=manifest" in failed.text
        assert str(container.workspace_root) not in failed.text
        assert str(source_rollout) in caplog.text
        assert failed.headers["X-Request-ID"] == failed.json()["request_id"]
        assert canonical.read_bytes() == original
        assert {item.session_id for item in (await container.session_service.list()).items} == original_sessions
        key = container.session_context_fork_service._creation_key(request.idempotency_key)
        record = container.session_service.path_resolver.catalog_store.get_creation_record(key)
        assert record.state == "aborted"
        assert str(container.workspace_root) not in str(asdict(record))
        retry = request.model_copy(update={"idempotency_key": f"fork-{uuid4().hex}"})
        recovered = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=retry.model_dump(mode="json"))
        assert recovered.status_code == 200, recovered.text
        result = recovered.json()["data"]
        target_session, target_thread = result["session"]["session_id"], result["thread_id"]
        with RolloutCheckpointSaver(context.saver._storage.storage.sessions_dir) as cold:
            assert cold._storage.storage.read_items(target_session, target_thread)
            target_node = cold._storage.storage.fork_paths.thread_node(target_session, target_thread)
            assert (target_node / "rollout/rollout.jsonl").read_bytes() == original
        assert canonical.read_bytes() == original
