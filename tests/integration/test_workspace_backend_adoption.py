from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.core.mounted_workspaces import (
    MountedWorkspace,
    mounted_workspace_manifest_digest,
)
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.runtime.local_workspace import start_managed_backend_runtime
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
    ManagedBackendOwner,
)


@dataclass(frozen=True, slots=True)
class _Backend:
    process: asyncio.subprocess.Process
    workspace_root: Path
    workspace_id: str
    port: int
    generation: str
    manifest_digest: str


@pytest.fixture
async def adopted_backend(
    integration_workspace_root_path: str,
) -> AsyncIterator[_Backend]:
    workspace_root = Path(integration_workspace_root_path)
    workspace_id = load_or_create_workspace_id(workspace_root)
    generation = "backend_generation_adoption"
    digest = mounted_workspace_manifest_digest((MountedWorkspace(workspace_id, workspace_root),))
    # 独立 HTTP 替身提供真实健康身份与进程组；父进程的 stdin 管道关闭时，
    # 服务也停止，进程外测试超时不会留下独立运行的替身。
    server_source = """
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

workspace_id, generation, digest = sys.argv[1:4]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/api/v1/health":
            self.send_error(404)
            return
        body = json.dumps({"workspace_id": workspace_id, "process_id": os.getpid(), "status": "ok", "backend_generation": generation, "mount_manifest_digest": digest}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

server = HTTPServer(("127.0.0.1", 0), Handler)

def watch_owner():
    sys.stdin.buffer.read()
    server.shutdown()

Thread(target=watch_owner, daemon=True).start()
print(server.server_address[1], flush=True)
server.serve_forever()
server.server_close()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        server_source,
        workspace_id,
        generation,
        digest,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        port_line = await asyncio.wait_for(process.stdout.readline(), timeout=3)
        assert port_line, "托管测试 Workspace API 未启动"
        yield _Backend(process, workspace_root, workspace_id, int(port_line), generation, digest)
    finally:
        assert process.stdin is not None
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_preferred_port_mismatch_rejects_without_stopping_adopted_backend(
    adopted_backend: _Backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = adopted_backend
    preferred_backend_port = (
        backend.port + 1 if backend.port < 65535 else backend.port - 1
    )

    def fail_if_started(**_: object) -> None:
        pytest.fail("接管端口不匹配时不应启动候选 Workspace API")

    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_backend_process",
        fail_if_started,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.local_workspace.start_local_node_service_process",
        fail_if_started,
    )

    owner = ManagedBackendOwner(ManagedBackendContract(
        "backend_owner_adoption", backend.generation,
        (ManagedBackendMember("registered-target", MountedWorkspace(backend.workspace_id, backend.workspace_root)),),
        f"http://127.0.0.1:{backend.port}",
    ))
    with pytest.raises(RuntimeError, match="端口与首选端口不匹配") as raised:
        await start_managed_backend_runtime(
            owner=owner, project_root=Path.cwd(), log_dir=tmp_path / "logs",
            preferred_backend_port=preferred_backend_port, adopt_existing_backend=True,
        )

    assert f"actual={backend.port}" in str(raised.value)
    assert f"preferred={preferred_backend_port}" in str(raised.value)
    assert backend.process.returncode is None
    async with httpx.AsyncClient(timeout=2) as client:
        response = await client.get(f"http://127.0.0.1:{backend.port}/api/v1/health")
    assert response.status_code == 200
    assert response.json()["workspace_id"] == backend.workspace_id
    assert response.json()["process_id"] == backend.process.pid
