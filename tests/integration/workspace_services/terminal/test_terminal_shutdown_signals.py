"""真实终端创建、PTY、HTTP与POSIX信号；显式屏障只延迟原Worker就绪回执。"""

from __future__ import annotations

import asyncio
import json
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.processes.workspace import terminate_process
from tests.support.workspaces import prepare_default_test_workspace

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="本组核对Linux原生PTY及POSIX信号；不代替Windows SIGBREAK验收")
TerminalService = tuple[TestRunContext, subprocess.Popen[str], str, str, Path, Path]


@pytest.fixture
def terminal_service(
    integration_backend_port: int, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TerminalService]:
    with TestRunContext.from_test_file(Path(__file__)) as run:
        root = prepare_default_test_workspace(
            workspace_root=run.workspace_root / uuid4().hex,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        control = run.artifacts_dir / "control"
        control.mkdir(parents=True)
        node = shutil.which("node")
        assert node is not None
        with monkeypatch.context() as env:
            env.setenv("NODE_OPTIONS", f"--import={(Path.cwd() / 'tests/integration/workspace_services/terminal/terminal_start_barrier.mjs').as_uri()}")
            env.setenv("BOXTEAM_TEST_PROJECT_ROOT", str(Path.cwd()))
            env.setenv("BOXTEAM_TEST_TERMINAL_CONTROL", str(control))
            process = start_terminal_backend(
                backend_port=integration_backend_port + 66, workspace_root=str(root), artifacts_dir=run.artifacts_dir,
            )
        try:
            yield run, process, f"http://127.0.0.1:{integration_backend_port + 66}", load_or_create_workspace_id(root), control, root
        finally:
            (control / "release").touch()
            try:
                terminate_process(process)
            finally:
                # Manager正常退出沿既有合同保留PTY；只回收本fixture记录的精确进程实例。
                lifecycle = (Path.cwd() / "src/workspace-services/terminal/server/processes/terminalProcessLifecycle.js").as_uri()
                script = f"""
import {{readFile}} from 'node:fs/promises';
import {{terminateTerminalProcessTree}} from {json.dumps(lifecycle)};
let raw;
try {{ raw = await readFile(process.argv[1], 'utf8'); }} catch (error) {{ if (error.code !== 'ENOENT') throw error; }}
for (const line of (raw ?? '').trim().split('\\n').filter(Boolean)) {{
  const identity = JSON.parse(line);
  const result = await terminateTerminalProcessTree(identity);
  console.log(JSON.stringify({{ identity, result }}));
}}
"""
                result = subprocess.run([node, "--input-type=module", "-e", script, str(control / "starts.jsonl")],
                                        capture_output=True, text=True, timeout=15, check=False)
                (run.artifacts_dir / "cleanup.stdout.raw").write_text(result.stdout)
                (run.artifacts_dir / "cleanup.stderr.raw").write_text(result.stderr)
                assert result.returncode == 0, result.stderr


async def wait_file(path: Path) -> str:
    async with asyncio.timeout(8):
        while not path.is_file():
            await asyncio.sleep(0.02)
        return path.read_text()


async def wait_diagnostic(run: TestRunContext, process: subprocess.Popen[str], marker: str) -> str:
    async with asyncio.timeout(8):
        while True:
            diagnostics = "\n".join(file.read_text() for file in run.artifacts_dir.glob("terminal-backend-*.stderr.log"))
            if marker in diagnostics:
                return diagnostics
            assert process.poll() is None, diagnostics
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT", "SIGHUP"])
async def test_signal_wait_timeout_keeps_original_creation_and_retryable_manager(
    terminal_service: TerminalService, signal_name: str,
) -> None:
    run, process, url, workspace_id, control, root = terminal_service
    node = shutil.which("node")
    terminal_id, peer_id, queued_id, late_id = [f"term_{uuid4().hex}" for _ in range(4)]
    payload = {"workspace_id": workspace_id, "owner_revision": 3, "command": node, "args": ["-e", "setInterval(() => {}, 1000)"]}
    headers = {"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-terminal"}
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=20) as client:
        peer = await client.post("/api/terminals", json=payload | {"terminal_id": peer_id})
        assert peer.status_code == 200, peer.text
        original = asyncio.create_task(client.post("/api/terminals", json=payload | {
            "terminal_id": terminal_id, "args": ["-e", "/* gated-terminal-start */ setInterval(() => {}, 1000)"],
        }))
        queued = None
        try:
            identity = json.loads(await wait_file(control / "arrived.json"))
            queued = asyncio.create_task(client.post("/api/terminals", json=payload | {"terminal_id": queued_id}))
            await asyncio.sleep(0.05)
            # 取正式创建生成的状态路径；不手填恢复记录。
            state_file = root / ".boxteam/terminal-manager/terminals.json"
            state_before = state_file.read_bytes()
            requested_signal = getattr(signal, signal_name)
            started = time.monotonic()
            process.send_signal(requested_signal)
            if signal_name == "SIGINT":
                await asyncio.sleep(0.1)
                process.send_signal(requested_signal)
            failure = "关闭失败，保留原任务与 HTTP 管理入口"
            diagnostics = await wait_diagnostic(run, process, failure)
            elapsed = time.monotonic() - started
            assert 4.5 <= elapsed < 8
            assert "terminal_manager_shutdown_timeout" in diagnostics
            assert diagnostics.count(failure) == 1
            assert process.poll() is None and not original.done() and not queued.done()
            current = await client.get(f"/api/terminals/{terminal_id}")
            assert current.status_code == 200, current.text
            assert current.json()["data"]["status"] == "created"
            assert current.json()["data"]["pty_worker_pid"] == identity["worker_pid"]
            peer_current = await client.get(f"/api/terminals/{peer_id}")
            assert peer_current.json()["data"]["os_pid"] == peer.json()["data"]["os_pid"]
            assert (await client.get("/health")).json()["terminal_count"] == 2
            assert state_file.read_bytes() == state_before
            assert not (control / "shutdowns.jsonl").exists()
            late = await client.post("/api/terminals", json=payload | {"terminal_id": late_id})
            assert late.status_code == 503, late.text
            assert late.json()["error"].startswith("Terminal Manager 正在关闭")
            assert late.json()["creation_receipt"] == {
                "resource_id": late_id, "workspace_id": workspace_id, "owner_revision": 3, "outcome": "not_created",
            }
            (control / "release").touch()
            created, refused = await asyncio.gather(original, queued)
            assert created.status_code == 200, created.text
            assert created.json()["data"]["os_pid"] == identity["pid"]
            assert refused.status_code == 503, refused.text
            assert refused.json()["creation_receipt"]["resource_id"] == queued_id
            process.send_signal(requested_signal)
            assert await asyncio.to_thread(process.wait, timeout=8) == 128 + requested_signal
            records = json.loads(state_file.read_text())["terminals"]
            assert {record["terminal_id"] for record in records} == {terminal_id, peer_id}
            assert all(record["reconciliation_status"] == "reconcile_required" for record in records)
            stops = [json.loads(line) for line in (control / "shutdowns.jsonl").read_text().splitlines()]
            assert len(stops) == 2 and all(stop["terminatePty"] is False for stop in stops)
            assert {stop["pid"] for stop in stops} == {identity["pid"], peer.json()["data"]["os_pid"]}
            cold = start_terminal_backend(
                backend_port=int(url.rsplit(":", 1)[1]), workspace_root=str(root), artifacts_dir=run.artifacts_dir,
            )
            try:
                restored = await client.get(f"/api/terminals/{terminal_id}")
                assert restored.status_code == 200, restored.text
                assert restored.json()["data"]["os_pid"] == identity["pid"]
                assert restored.json()["data"]["pty_worker_pid"] is None
            finally:
                terminate_process(cold)
            run.write_result("passed", {"signal": signal_name, "wait_seconds": elapsed, "identity": identity,
                                       "scope": "真实PTY/HTTP/POSIX信号及冷Manager；就绪交付屏障，不含Gateway/Web、Windows或完整进程退出期限"})
        finally:
            (control / "release").touch()
            await asyncio.gather(*[task for task in (original, queued) if task is not None], return_exceptions=True)


@pytest.mark.parametrize(("fatal_kind", "signal_first"), [("exception", False), ("rejection", False), ("exception", True)])
async def test_fatal_error_during_creation_wait_still_exits_one(
    terminal_service: TerminalService, fatal_kind: str, signal_first: bool,
) -> None:
    run, process, url, workspace_id, control, _root = terminal_service
    async with httpx.AsyncClient(base_url=url, timeout=20, headers={
        "X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-terminal",
    }) as client:
        creation = asyncio.create_task(client.post("/api/terminals", json={
            "workspace_id": workspace_id, "terminal_id": f"term_{uuid4().hex}", "owner_revision": 3,
            "command": shutil.which("node"), "args": ["-e", "/* gated-terminal-start */ setInterval(() => {}, 1000)"],
        }))
        try:
            await wait_file(control / "arrived.json")
            if signal_first:
                process.send_signal(signal.SIGTERM)
                await asyncio.sleep(0.1)
            (control / "fatal").write_text(fatal_kind)
            diagnostics = await wait_diagnostic(run, process, "致命错误后的关闭失败")
            assert f"terminal-test-fatal-{fatal_kind}" in diagnostics
            assert "terminal_manager_shutdown_timeout" in diagnostics
            assert "保留原任务与 HTTP 管理入口" not in diagnostics
            (control / "release").touch()
            assert await asyncio.to_thread(process.wait, timeout=8) == 1
            run.write_result("passed", {"fatal_kind": fatal_kind, "signal_first": signal_first, "exit_code": 1})
        finally:
            (control / "release").touch()
            await asyncio.gather(creation, return_exceptions=True)
