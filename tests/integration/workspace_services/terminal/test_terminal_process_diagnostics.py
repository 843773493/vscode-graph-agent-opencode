"""真实管理停止的原生进程失败只在诊断中保留原文，修复后沿原身份重试。"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.processes.workspace import terminate_process
from tests.support.workspaces import prepare_default_test_workspace

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="真实Linux进程元数据与PTY；不代其它平台验收")
TerminalService = tuple[str, str, Path, Path, TestRunContext, list[psutil.Process]]


@pytest.fixture
def terminal_service(integration_backend_port: int, monkeypatch: pytest.MonkeyPatch) -> Iterator[TerminalService]:
    with TestRunContext.from_test_file(Path(__file__)) as run:
        root = prepare_default_test_workspace(
            workspace_root=run.workspace_root / uuid4().hex,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        control = run.artifacts_dir / "control"
        control.mkdir()
        (control / "log-parent").write_text("真实ENOTDIR", encoding="utf-8")
        preload = control / "process-diagnostics.mjs"
        preload.write_text("""
import fs from "node:fs";
import nativeFs from "node:fs/promises";
import path from "node:path";
import { syncBuiltinESMExports } from "node:module";

// 只在原生读取、扫描、信号及原日志writer边界注入故障，不代填产品状态。
if (path.basename(process.argv[1] || "") === "backend.js") {
  const control = process.env.BOXTEAM_TEST_TERMINAL_PROCESS_CONTROL;
  const marker = path.join(control, "fault.json");
  const fault = () => fs.existsSync(marker) ? JSON.parse(fs.readFileSync(marker, "utf8")) : null;
  const privatePath = path.join(control, "private-process", "stat");
  const failure = code => Object.assign(new Error(`${code}: 原生进程访问失败 '${privatePath}'`), { code, path: privatePath });
  const read = nativeFs.readFile;
  nativeFs.readFile = async (file, ...args) => {
    const current = fault();
    if (current && file === `/proc/${current.pid}/stat`) {
      if (current.stage === "read") throw failure("EACCES");
      if (current.stage === "parse") return privatePath;
    }
    return await read(file, ...args);
  };
  const scan = nativeFs.readdir;
  nativeFs.readdir = async (directory, ...args) => {
    if (fault()?.stage === "scan" && directory === "/proc") throw failure("EACCES");
    return await scan(directory, ...args);
  };
  const kill = process.kill.bind(process);
  process.kill = (pid, signal) => {
    const current = fault();
    if (current?.stage === "signal" && pid === current.pid && signal === "SIGTERM") throw failure("EPERM");
    return kill(pid, signal);
  };
  const write = process.stderr.write.bind(process.stderr);
  process.stderr.write = (chunk, ...args) => {
    if (typeof chunk === "string" && chunk.includes("[terminal-process]")) {
      fs.appendFileSync(path.join(control, "attempts.jsonl"), JSON.stringify({ chunk }) + "\\n");
      if (fs.existsSync(path.join(control, "fail-sink"))) fs.appendFileSync(path.join(control, "log-parent/diagnostic.log"), chunk);
    }
    return write(chunk, ...args);
  };
  syncBuiltinESMExports();
}
""", encoding="utf-8")
        with monkeypatch.context() as env:
            env.setenv("NODE_OPTIONS", f"{os.environ.get('NODE_OPTIONS', '')} --import={preload.as_uri()}".strip())
            env.setenv("BOXTEAM_TEST_TERMINAL_PROCESS_CONTROL", str(control))
            port = integration_backend_port + 63
            backend = start_terminal_backend(backend_port=port, workspace_root=str(root), artifacts_dir=run.artifacts_dir)
        tracked: list[psutil.Process] = []
        try:
            yield f"http://127.0.0.1:{port}", load_or_create_workspace_id(root), root, control, run, tracked
        finally:
            try:
                terminate_process(backend)
            finally:
                for observed in tracked:
                    with suppress(psutil.NoSuchProcess):
                        if observed.is_running() and observed.status() != psutil.STATUS_ZOMBIE:
                            observed.kill()


@pytest.mark.parametrize("stage", ["read", "parse", "scan", "signal"])
@pytest.mark.parametrize("fail_sink", [False, True], ids=["healthy-log", "failed-log"])
async def test_native_process_failure_keeps_unknown_identity_and_management_retry(
    terminal_service: TerminalService, stage: str, fail_sink: bool,
) -> None:
    url, workspace_id, root, control, run, observed_processes = terminal_service
    terminal_id = f"term_{uuid4().hex}"
    headers = {"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-terminal"}
    marker = control / "fault.json"
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=15, trust_env=False) as client, AsyncExitStack() as cleanup:
        created = await client.post("/api/terminals", json={
            "terminal_id": terminal_id, "workspace_id": workspace_id, "owner_revision": 7,
            "command": sys.executable, "args": ["-c", "import time; time.sleep(120)"],
        })
        assert created.status_code == 200, created.text
        original = created.json()["data"]
        observed_processes.extend(psutil.Process(original[key]) for key in ("os_pid", "pty_worker_pid"))
        cleanup.push_async_callback(client.delete, f"/api/terminals/{terminal_id}")
        cleanup.callback(marker.unlink, missing_ok=True)
        marker.write_text(json.dumps({"stage": stage, "pid": original["os_pid"]}), encoding="utf-8")
        if fail_sink:
            (control / "fail-sink").touch()
        failed = await client.post(f"/api/terminals/{terminal_id}/kill", json={})
        assert failed.status_code == 500, failed.text
        error = failed.json()["error"]
        assert error.startswith("终端进程操作失败:"), failed.text
        assert (f"process_session_id={original['process_session_id']}" if stage == "scan" else f"pid={original['os_pid']}") in error
        assert ("code=EPERM" if stage == "signal" else "code=EACCES" if stage != "parse" else "type=Error") in error
        current_response = await client.get(f"/api/terminals/{terminal_id}")
        assert current_response.status_code == 200, current_response.text
        current = current_response.json()["data"]
        assert current["status"] == "running" and current["reconciliation_status"] == "unknown"
        for key in ("os_pid", "pty_worker_pid", "process_group_id", "process_session_id", "process_start_time", "transport_owner_revision", "created_at"):
            assert current[key] == original[key]
        assert all(process.is_running() and process.status() != psutil.STATUS_ZOMBIE for process in observed_processes)
        state_file = root / ".boxteam/terminal-manager/terminals.json"
        persisted = json.loads(state_file.read_text())["terminals"][0]
        assert all(current[key] == value for key, value in persisted.items())
        attempts = [json.loads(line)["chunk"] for line in (control / "attempts.jsonl").read_text().splitlines()]
        assert len(attempts) == 1
        assert str(control / "private-process/stat") in attempts[0]
        for payload in (failed.json(), current, persisted):
            serialized = json.dumps(payload)
            for private in (control / "private-process/stat", control / "log-parent"):
                assert json.dumps(str(private))[1:-1] not in serialized
        marker.unlink()
        (control / "fail-sink").unlink(missing_ok=True)
        stopped = await client.post(f"/api/terminals/{terminal_id}/kill", json={})
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["data"]["killed"] is True
        terminal = stopped.json()["data"]["terminal"]
        assert terminal["reconciliation_status"] == "verified" and terminal["status"] == "terminated"
        assert terminal["os_pid"] == original["os_pid"] and terminal["transport_owner_revision"] == 7
        assert all(not process.is_running() or process.status() == psutil.STATUS_ZOMBIE for process in observed_processes)
        run.write_result("passed", {"stage": stage, "fail_sink": fail_sink, "original": original,
                                   "failure": failed.json(), "unknown": current, "stopped": terminal,
                                   "scope": "真实HTTP/PTY、原生失败及同身份管理重试；不代Gateway/Web、Python lease、Backend重启或Windows"})
