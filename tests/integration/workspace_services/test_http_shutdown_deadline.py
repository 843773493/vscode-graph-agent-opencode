"""真实辅助服务信号关闭：未完成请求体不无限阻断已排空 Manager 的退出。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.process_output import spawn_logged_process
from tests.harness.python.run_context import TestRunContext
from tests.support.processes.workspace import (
    ensure_local_port_available,
    terminate_process,
    wait_for_http_ok,
)
from tests.support.workspaces import prepare_test_workspace

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM；Windows SIGBREAK 另验")


@pytest.fixture(params=["terminal", "browser"])
def service(
    request: pytest.FixtureRequest, integration_backend_port: int,
) -> Iterator[tuple[str, subprocess.Popen[str], int, str, Path, TestRunContext]]:
    kind = request.param
    with TestRunContext.from_test_file(Path(request.node.path)) as run:
        root = prepare_test_workspace(
            workspace_root=run.workspace_root,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=(),
        )
        workspace_id = load_or_create_workspace_id(root)
        node = shutil.which("node")
        assert node is not None
        ensure_local_port_available(integration_backend_port, service_name=f"{kind} HTTP关闭")
        process, _logs = spawn_logged_process(
            [node, str(Path.cwd() / f"src/workspace-services/{kind}/server/backend.js"),
             "--host", "127.0.0.1", "--port", str(integration_backend_port),
             "--workspace-root", str(root), "--workspace-id", workspace_id],
            cwd=Path.cwd(), env={**os.environ, "BOXTEAM_PROJECT_ROOT": str(Path.cwd())},
            artifacts_dir=run.artifacts_dir, log_name=f"{kind}-backend",
        )
        run.add_cleanup("辅助服务进程", lambda: terminate_process(process))
        wait_for_http_ok(f"http://127.0.0.1:{integration_backend_port}/health", process)
        yield kind, process, integration_backend_port, workspace_id, root, run


@pytest.mark.parametrize("incomplete_body", [False, True], ids=["clean", "incomplete-body"])
async def test_signal_exit_bounds_http_drain_and_reports_timeout(
    service: tuple[str, subprocess.Popen[str], int, str, Path, TestRunContext],
    incomplete_body: bool,
) -> None:
    kind, process, port, workspace_id, root, run = service
    writer = None
    try:
        if incomplete_body:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write((
                f"POST /api/{kind}s HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"X-BoxTeam-Workspace-Id: {workspace_id}\r\n"
                f"X-BoxTeam-Resource-Owner: workspace-{kind}\r\n"
                "Content-Type: application/json\r\nContent-Length: 100000\r\n"
                "Expect: 100-continue\r\nConnection: keep-alive\r\n\r\n"
            ).encode())
            await writer.drain()
            assert await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2) == b"HTTP/1.1 100 Continue\r\n\r\n"
            writer.write(b'{"title":')
            await writer.drain()
        async with httpx.AsyncClient(trust_env=False) as client:
            health = await client.get(f"http://127.0.0.1:{port}/health")
        assert health.status_code == 200
        assert health.json()[f"{kind}_count"] == 0
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        exit_code = await asyncio.to_thread(process.wait, 8)
        elapsed = time.monotonic() - started
        diagnostics = "\n".join(path.read_text() for path in run.artifacts_dir.glob("*.stderr.log"))
        if incomplete_body:
            assert 4.5 <= elapsed < 8
            assert exit_code == 1
            assert "http_server_drain_timeout" in diagnostics
            assert f"[{kind}-backend] transport 关闭失败" in diagnostics
        else:
            assert exit_code == 143
            assert elapsed < 5
            assert "http_server_drain_timeout" not in diagnostics
        state = json.loads((root / f".boxteam/{kind}-manager/{kind}s.json").read_text())
        assert state[f"{kind}s"] == [], "不完整的创建请求不得产生资源或恢复记录"
        (run.artifacts_dir / "http-shutdown.json").write_text(json.dumps({
            "kind": kind, "incomplete_body": incomplete_body, "exit_code": exit_code,
            "elapsed_seconds": elapsed, "workspace_id": workspace_id,
            "scope": "真实 Node/HTTP/POSIX 信号；空 Manager，不代替资源排空、Gateway/Web 或 Windows 验收",
        }, ensure_ascii=False, indent=2) + "\n")
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
