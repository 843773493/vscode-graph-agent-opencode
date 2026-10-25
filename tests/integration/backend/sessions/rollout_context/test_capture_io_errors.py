"""产品生成来源后，冻结 marker 发布与读取错误不能泄漏路径或发布半个目标。"""

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


@pytest.mark.parametrize("fault", ["scan", "marker_write", "marker_replace", "marker_read", "manifest_read", "control_read"])
async def test_public_fork_capture_io_failure_keeps_original_and_can_retry(
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
    failures: list[PermissionError] = []
    owner, operation = {
        "scan": (os, "scandir"), "marker_write": (Path, "open"), "marker_replace": (os, "replace"),
        "marker_read": (Path, "read_bytes"), "manifest_read": (Path, "read_bytes"), "control_read": (Path, "open"),
    }[fault]
    original_operation = getattr(owner, operation)

    def denied(value, *args, **kwargs):
        if not isinstance(value, int):
            path = Path(value)
            captured = "fork-source-captures" in path.parts
            selected = captured and (
                (fault == "scan" and path.parent.name == "fork-source-captures")
                or (fault in {"marker_write", "marker_replace"} and path.name == "captured.preparing")
                or (fault == "marker_read" and path.name == "captured.json")
                or (fault == "manifest_read" and path.name == "snapshot-manifest.json")
                or (fault == "control_read" and path.name == "session-control.sqlite"
                    and (path.parent.parent / "captured.json").is_file())
            )
            if selected and not failures:
                error = PermissionError(errno.EACCES, f"原生捕获故障 {path}", str(path))
                failures.append(error)
                raise error
        return original_operation(value, *args, **kwargs)

    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(sessions.router, prefix="/api/v1")
    app.dependency_overrides[sessions.verify_local_token] = lambda: "test-local-token"
    app.dependency_overrides[sessions.get_session_context_fork_service] = lambda: container.session_context_fork_service
    headers = {"X-BoxTeam-Gateway-Id": get_current_gateway_id()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workspace", headers=headers) as client:
        with monkeypatch.context() as patch:
            patch.setattr(owner, operation, denied)
            failed = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        assert failed.status_code == 500, failed.text
        stage = "publication" if fault in {"scan", "marker_write", "marker_replace"} else fault
        assert "artifact=fork_capture stage=" + stage in failed.text
        assert len(failures) == 1
        assert "PermissionError" in failed.text and "errno=13" in failed.text
        assert str(container.workspace_root) not in failed.text
        assert failures[0].filename in caplog.text
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
