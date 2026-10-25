"""产品生成执行与调试方案后，公开 fork 的文件错误不得泄漏真实路径。"""

from __future__ import annotations

import errno
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import sessions
from app.container import AppContainer
from app.core.trace_middleware import TraceMiddleware, get_current_gateway_id
from app.schemas.internal_v2.node_debug import NodeDebugConfigurationCreateRequest
from app.schemas.internal_v2.session import (
    SessionCreateRequest,
    SessionForkResourceChoice,
)
from app.services.infrastructure.node_debug.session.fork import (
    debug_configuration_resource_uri,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _main_thread_id,
    _main_thread_node,
    _production_fork_context,
    fork_gateway_request_context,
    production_fork_container,
)

__all__ = ["fork_execution_message", "fork_gateway_request_context", "production_fork_container"]


@pytest.mark.parametrize("fault", ["staging", "publication", "verification"])
async def test_public_debug_fork_failure_preserves_source_and_safe_error(
    production_fork_container: AppContainer, fork_execution_message,
    fault: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    container = production_fork_container
    context = _production_fork_context(container)
    source = await container.session_service.create(SessionCreateRequest(title="真实调试方案 fork 来源"))
    await fork_execution_message(container, source.session_id, content="生成可恢复来源")
    source_thread = _main_thread_id(context, source.session_id)
    (container.workspace_root / "app.mjs").write_text("console.log('debug-fork');\n")
    state = await container.node_debug_service.create_configuration(NodeDebugConfigurationCreateRequest(
        session_id=source.session_id, thread_id=source_thread, name="待复制方案", script_path="app.mjs",
        launch_profile_name="node-default", activate=False,
    ))
    summary, = state.configurations
    configuration = context.saver._node_debug_store.read_configuration(
        source.session_id, summary.configuration_id, source_thread,
    )
    assert configuration is not None
    assert summary.updated_at == configuration.updated_at
    assert container.node_debug_service.get_configuration(
        source.session_id, summary.configuration_id, source_thread,
    ) == configuration
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy")
    request = request.model_copy(update={"resources": [SessionForkResourceChoice(
        resource_uri=debug_configuration_resource_uri(request.source_uri, configuration.configuration_id),
        action="copy_snapshot",
    )]})
    source_node = _main_thread_node(context, source.session_id)
    canonical = source_node / "rollout/rollout.jsonl"
    original = canonical.read_bytes()
    source_debug = {path.relative_to(source_node): path.read_bytes() for path in (source_node / "debug/node").rglob("*.json")}
    original_items = context.saver._storage.storage.read_items(source.session_id, source_thread)
    original_sessions = {item.session_id for item in (await container.session_service.list()).items}
    failed_paths: list[Path] = []
    operation = {"staging": "mkdir", "publication": "rename", "verification": "read_bytes"}[fault]
    native = getattr(Path, operation)
    errors: list[PermissionError] = []

    def fail_once(path: Path, *args, **kwargs):
        selected = (
            (fault == "staging" and path.name == "node" and ".fork-debug-staging" in path.parts)
            or (fault == "publication" and ".fork-debug-staging" in path.parts and path.name == "node")
            or (fault == "verification" and path.parts[-3:] == ("debug", "node", "manifest.json") and source_node not in path.parents)
        )
        if selected and not errors:
            error = PermissionError(errno.EACCES, f"真实文件故障 {path}", str(path))
            errors.append(error)
            failed_paths.append(path)
            raise error
        return native(path, *args, **kwargs)

    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(sessions.router, prefix="/api/v1")
    app.dependency_overrides[sessions.verify_local_token] = lambda: "test-local-token"
    app.dependency_overrides[sessions.get_session_context_fork_service] = lambda: container.session_context_fork_service
    headers = {"X-BoxTeam-Gateway-Id": get_current_gateway_id()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://workspace", headers=headers) as client:
        with monkeypatch.context() as patch:
            patch.setattr(Path, operation, fail_once)
            response = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        assert response.status_code == 500, response.text
        assert len(errors) == 1
        assert f"artifact=fork_debug stage={fault}" in response.text
        assert "PermissionError" in response.text and "errno=13" in response.text
        assert str(container.workspace_root) not in response.text
        assert str(failed_paths[0]) in caplog.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        assert canonical.read_bytes() == original
        assert context.saver._storage.storage.read_items(source.session_id, source_thread) == original_items
        assert all((source_node / locator).read_bytes() == raw for locator, raw in source_debug.items())
        assert {item.session_id for item in (await container.session_service.list()).items} == original_sessions
        key = container.session_context_fork_service._creation_key(request.idempotency_key)
        record = container.session_service.path_resolver.catalog_store.get_creation_record(key)
        assert record.state == "aborted"
        assert str(container.workspace_root) not in json.dumps(asdict(record), default=str)
        retry = request.model_copy(update={"idempotency_key": f"fork-{uuid4().hex}"})
        recovered = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=retry.model_dump(mode="json"))
        assert recovered.status_code == 200, recovered.text
        result = recovered.json()["data"]
        target_session = result["session"]["session_id"]
        target_thread = result["thread_id"]
        with RolloutCheckpointSaver(context.saver._storage.storage.sessions_dir) as cold:
            assert cold._storage.storage.read_items(target_session, target_thread)
            manifest = cold._node_debug_store.read_manifest(target_session, target_thread)
            assert manifest is not None and manifest.active_configuration_id is None and manifest.actions == []
            copied, = cold._node_debug_store.list_configurations(target_session, target_thread)
            assert copied.configuration_id != configuration.configuration_id
            assert copied.model_copy(update={"configuration_id": configuration.configuration_id}) == configuration
        assert canonical.read_bytes() == original
