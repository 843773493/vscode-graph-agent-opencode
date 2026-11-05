"""会话恢复读取登记归属，显式健康检查仍报告其它目标的真实超时。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.http_server import running_http_server
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture(autouse=True)
def metadata_web_assets(monkeypatch: pytest.MonkeyPatch, web_workbench_assets: Path) -> None:
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))


@pytest.fixture
def other_target(request: pytest.FixtureRequest) -> Iterator[tuple[Path, str, list[str]]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root / context.run_root.name / "health-B",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        fresh_identity=True,
    )
    available = Event()
    available.set()
    reads: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/block":
                available.clear()
                body: object = {"blocked": True}
            elif self.path == "/release":
                available.set()
                body = {"blocked": False}
            else:
                reads.append(self.path)
                if not available.is_set():
                    # 故障只在 B 的网络边界；真实 Gateway 按原 HTTP 超时判离线。
                    available.wait(timeout=15)
                    self.close_connection = True
                    return
                if self.path == "/api/v1/health":
                    identity = json.loads((workspace / ".boxteam/workspace-identity.json").read_text())
                    body = {"status": "ok", "workspace_id": identity["workspace_id"]}
                elif self.path == "/api/v1/config/reload-status":
                    body = {"data": {"healthy": True}}
                else:
                    raise AssertionError(f"B 不应接收业务请求：{self.path}")
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    with running_http_server(server):
        try:
            yield workspace, f"http://127.0.0.1:{server.server_port}", reads
        finally:
            available.set()
            (context.artifacts_dir / "other-target-reads.json").write_text(json.dumps(reads, indent=2) + "\n")


async def test_thread_restore_does_not_probe_unrelated_target(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
    other_target: tuple[Path, str, list[str]],
) -> None:
    client, context, _workspace, provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "会话恢复验收需要 Chromium"
    other, backend_url, reads = other_target
    registered = await client.post("/api/gateway/workspaces/local", json={
        "root_path": str(other), "name": "Health B", "backend_url": backend_url,
    })
    assert registered.status_code == 200, registered.text
    assert registered.json()["request_id"] == registered.headers["X-Request-ID"]
    target = next(item for item in registered.json()["data"]["items"] if item["name"] == "Health B")
    assert target["status"] == "ready"
    result_path = context.artifacts_dir / "thread-restore-health.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "120", "--",
               "bun", "tests/integration/clients/web/quick_access/thread_restore_health.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_HEALTH_OTHER_URL": backend_url,
                 "BOXTEAM_HEALTH_OTHER_ID": target["workspace_id"], "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=135, check=False)
    for name, text in (("browser.stdout.raw", completed.stdout), ("browser.stderr.raw", completed.stderr)):
        (context.artifacts_dir / name).write_text(text, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, indent=2) + "\n")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(result_path.read_text())
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert result["health"]["status"] == "offline"
    assert "/api/v1/health" in reads
    assert set(reads) <= {"/api/v1/health", "/api/v1/config/reload-status"}
    with provider.control._condition:
        calls = {key: count for key, count in provider.control._counters.items() if key.startswith("model_call/")}
    assert calls == {}, "会话恢复不应触发模型请求"
