"""真实 Node 握手中接收失败的 HTTP、持久动作与冷读。"""

from __future__ import annotations

import errno
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
import websockets
from fastapi import FastAPI, HTTPException
from websockets.asyncio.client import ClientConnection

from app.api.deps import get_node_debug_service, verify_local_token
from app.api.node_debug import router
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.main import workspace_http_exception_handler
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.process.inspector import NodeDebugInspector
from app.services.infrastructure.node_debug.process.process_identity import (
    probe_process_identity,
)
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.node_debug_dependencies import (
    node_debug_file_paths,
    permissive_node_debug_session_admission,
)
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace

DebugReceiveClient = tuple[
    httpx.AsyncClient, NodeDebugSessionStore, dict[str, str], Path, Callable[[], Awaitable[None]],
]


@pytest.fixture
async def debug_receive_client(integration_workspace_root_path: str) -> AsyncIterator[DebugReceiveClient]:
    root = Path(integration_workspace_root_path)
    with build_catalog_workspace(root.parent, workspace_id=load_or_create_workspace_id(root)) as workspace:
        session_id = await workspace.create_session("Inspector 接收错误")
        owner = {"session_id": session_id, "thread_id": workspace.resolver.main_thread_id(session_id)}
        (root / "entry.mjs").write_text("setInterval(() => {}, 1000);\n")
        store = NodeDebugSessionStore(workspace.resolver)
        services: list[NodeDebugService] = []

        def service() -> NodeDebugService:
            created = NodeDebugService(
                workspace_root=root, file_paths=node_debug_file_paths(root),
                config_service=ConfigService(workspace_root=root), session_store=store,
                session_admission=permissive_node_debug_session_admission(),
                external_resource_leases=ExternalResourceLeaseLedger(),
            )
            services.append(created)
            return created

        app = FastAPI()
        app.add_exception_handler(HTTPException, workspace_http_exception_handler)
        app.add_middleware(TraceMiddleware)
        app.include_router(router, prefix="/api/v1")
        current = service()
        app.dependency_overrides[get_node_debug_service] = lambda: current
        app.dependency_overrides[verify_local_token] = lambda: "local"
        async def reopen() -> None:
            nonlocal current
            await current.close()
            current = service()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            try:
                yield client, store, owner, root, reopen
            finally:
                for created in reversed(services):
                    await created.close()



@pytest.mark.asyncio
async def test_handshake_receive_failure_keeps_native_cause_private_and_persists_actions(
    debug_receive_client: DebugReceiveClient,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    native_error = PermissionError(errno.EACCES, "Inspector 接收边界故障", str(root / "inspector-receive-secret"))
    original_connect = websockets.connect
    opened: list[ClientConnection] = []

    class FailingReceiver:
        def __init__(self, socket: ClientConnection) -> None:
            self.socket = socket
            self.failure: PermissionError | None = native_error

        async def send(self, message: str) -> None:
            await self.socket.send(message)

        async def close(self) -> None:
            await self.socket.close()

        def __aiter__(self) -> AsyncIterator[str | bytes]:
            async def messages() -> AsyncIterator[str | bytes]:
                async for message in self.socket:
                    if self.failure is not None:
                        error = self.failure
                        self.failure = None
                        raise error
                    yield message
            return messages()

    async def connect(*args: object, **kwargs: object) -> FailingReceiver:
        socket = await original_connect(*args, **kwargs)
        opened.append(socket)
        return FailingReceiver(socket)

    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "原方案接收失败后重试", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    payload = {**owner, "configuration_id": configuration_id, "path": "entry.mjs"}
    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR, logger=NodeDebugInspector.__module__):
        patch.setattr(websockets, "connect", connect)
        failed = await client.post("/api/v1/debug/node/start", json=payload)
    assert failed.status_code == 409, failed.text
    assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
    assert "PermissionError" in failed.text and "errno=13" in failed.text
    assert owner["session_id"] in failed.text and owner["thread_id"] in failed.text
    assert "inspector-receive-secret" not in failed.text
    assert any(record.exc_info and record.exc_info[1] is native_error for record in caplog.records)
    assert len(opened) == 1 and opened[0].close_code is not None
    state = await client.get("/api/v1/debug/node", params=owner)
    assert state.status_code == 200, state.text
    data = state.json()["data"]
    assert data["status"] == "failed" and isinstance(data["pid"], int)
    assert probe_process_identity(data["pid"]) is None
    assert data["error_message"] == failed.json()["detail"]
    failures = [action for action in data["actions"] if action["action"] == "start_failed"]
    assert len(failures) == 1 and failures[0]["message"] == data["error_message"]
    manifest_bytes, manifest = store.read_manifest_payload(**owner)
    assert manifest_bytes is not None and manifest is not None
    assert "inspector-receive-secret" not in manifest_bytes.decode()
    assert [action.model_dump(mode="json") for action in manifest.actions] == data["actions"]
    claim = store.read_launch_claim(**owner)
    assert claim is not None and claim.phase == "settled" and claim.pid == data["pid"]

    await reopen()
    cold = await client.get("/api/v1/debug/node", params=owner)
    assert cold.status_code == 200, cold.text
    restored = cold.json()["data"]
    assert restored["status"] == "idle" and restored["pid"] is None
    assert restored["active_configuration_id"] == configuration_id
    assert restored["actions"] == data["actions"]
    assert "inspector-receive-secret" not in cold.text

    retried = await client.post("/api/v1/debug/node/start", json=payload)
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["status"] == "running"
    assert retried.json()["data"]["active_configuration_id"] == configuration_id
    stopped = await client.post("/api/v1/debug/node/action", json={**owner, "action": "stop"})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["data"]["status"] == "exited"
    assert [action for action in stopped.json()["data"]["actions"] if action["action"] == "start_failed"] == failures
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / "receive-errors.json").write_text(json.dumps({
        "failed": failed.json(), "live": state.json(), "manifest": manifest.model_dump(mode="json"),
        "cold": cold.json(), "retried": retried.json(), "stopped": stopped.json(),
    }, ensure_ascii=False, indent=2) + "\n")
