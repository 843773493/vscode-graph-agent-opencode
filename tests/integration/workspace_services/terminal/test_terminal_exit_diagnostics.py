"""真实Worker错误与诊断writer次错不能跳过PTY停止及退出状态发布。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import suppress
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

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="真实Linux PTY父子及信号；不代替Windows验收")
TerminalService = tuple[TestRunContext, subprocess.Popen[str], str, str, Path, Path, list[psutil.Process]]


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
        preload = control / "exit-diagnostics.mjs"
        preload.write_text("""
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

// 故障仅在真实Worker错误事件及原诊断writer；不替换停止、持久化或进程身份。
if (path.basename(process.argv[1] || "") === "backend.js") {
  const control = process.env.BOXTEAM_TEST_TERMINAL_EXIT_CONTROL;
  const root = process.env.BOXTEAM_TEST_PROJECT_ROOT;
  process.on("unhandledRejection", reason => {
    // 只观察原致命处理器开始前的产品文件，不处理或改变拒绝。
    const state = JSON.parse(readFileSync(path.join(process.env.WORKSPACE_ROOT,
      ".boxteam/terminal-manager/terminals.json"), "utf8"));
    writeFileSync(path.join(control, "before-fatal.json"), JSON.stringify({ message: reason.message, state }));
  });
  const write = process.stderr.write.bind(process.stderr);
  process.stderr.write = (chunk, ...args) => {
    if (typeof chunk === "string" && chunk.includes("[terminal-session]")) {
      appendFileSync(path.join(control, "attempts.jsonl"), JSON.stringify({ chunk }) + "\\n");
      if (existsSync(path.join(control, "fail-sink"))) {
        appendFileSync(path.join(control, "log-parent/diagnostic.log"), chunk);
      }
    }
    return write(chunk, ...args);
  };
  const { IsolatedPtyProcess } = await import(pathToFileURL(path.join(root,
    "src/workspace-services/terminal/server/processes/isolatedPtyProcess.js")));
  const start = IsolatedPtyProcess.prototype.start;
  IsolatedPtyProcess.prototype.start = async function (options) {
    const ready = await start.call(this, options);
    const worker = this.workerProcess;
    appendFileSync(path.join(control, "starts.jsonl"), JSON.stringify({ pid: ready.pid, worker_pid: worker.pid }) + "\\n");
    const kill = process.kill.bind(process);
    let rejectedStop = false;
    process.kill = (pid, signal) => {
      if (!rejectedStop && pid === ready.pid && signal === "SIGTERM" && existsSync(path.join(control, "fail-stop"))) {
        rejectedStop = true;
        throw Object.assign(new Error(`受控首次停止失败: ${control}/private-stop`), { code: "EPERM" });
      }
      return kill(pid, signal);
    };
    const interval = setInterval(() => {
      if (!existsSync(path.join(control, "fail-worker"))) return;
      clearInterval(interval);
      worker.emit("error", new Error(`受控Worker I/O失败: ${control}/private-pty`));
    }, 10);
    interval.unref();
    return ready;
  };
}
""", encoding="utf-8")
        with monkeypatch.context() as env:
            env.setenv("NODE_OPTIONS", f"{os.environ.get('NODE_OPTIONS', '')} --import={preload.as_uri()}".strip())
            env.setenv("BOXTEAM_TEST_PROJECT_ROOT", str(Path.cwd()))
            env.setenv("BOXTEAM_TEST_TERMINAL_EXIT_CONTROL", str(control))
            process = start_terminal_backend(
                backend_port=integration_backend_port + 63, workspace_root=str(root), artifacts_dir=run.artifacts_dir,
            )
        tracked: list[psutil.Process] = []
        try:
            yield run, process, f"http://127.0.0.1:{integration_backend_port + 63}", load_or_create_workspace_id(root), root, control, tracked
        finally:
            try:
                terminate_process(process)
            finally:
                # 产品停止证明失败时才兜底回收本例已冻结创建时间的精确实例。
                for observed in tracked:
                    with suppress(psutil.NoSuchProcess):
                        if executing(observed):
                            observed.kill()


def executing(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
    except psutil.NoSuchProcess:
        return False


@pytest.mark.parametrize("fail_sink", [False, True], ids=["healthy-log", "failed-log"])
async def test_worker_error_stops_real_tree_and_persists_before_diagnostic_failure_exit(
    terminal_service: TerminalService, fail_sink: bool,
) -> None:
    run, backend, url, workspace_id, root, control, tracked = terminal_service
    terminal_id = f"term_{uuid4().hex}"
    workload = f"""
import json, os, signal
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
child = os.fork()
if child == 0:
    while True: signal.pause()
Path({str(control / 'workload.pending')!r}).write_text(json.dumps({{'child_pid': child}}))
Path({str(control / 'workload.pending')!r}).replace({str(control / 'workload.json')!r})
while True: signal.pause()
"""
    headers = {"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-terminal"}
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=15, trust_env=False) as client:
        created = await client.post("/api/terminals", json={
            "terminal_id": terminal_id, "workspace_id": workspace_id, "owner_revision": 4,
            "command": sys.executable, "args": ["-c", workload],
        })
        assert created.status_code == 200, created.text
        snapshot = created.json()["data"]
        tracked.extend([psutil.Process(snapshot["os_pid"]), psutil.Process(snapshot["pty_worker_pid"])])
        async with asyncio.timeout(8):
            while not (control / "workload.json").exists():
                await asyncio.sleep(0.02)
        tracked.append(psutil.Process(json.loads((control / "workload.json").read_text())["child_pid"]))
        assert all(executing(process) for process in tracked)
        identities = [{"pid": process.pid, "created_at": process.create_time()} for process in tracked]
        if fail_sink:
            (control / "fail-sink").touch()
        (control / "fail-worker").touch()
        if fail_sink:
            assert await asyncio.to_thread(backend.wait, timeout=10) == 1
            before_fatal = json.loads((control / "before-fatal.json").read_text())
            assert before_fatal["state"]["terminals"][0]["status"] == "exited"
            assert before_fatal["state"]["terminals"][0]["release_reason"] == "pty_exit_cleanup_failed"
        else:
            async with asyncio.timeout(8):
                while True:
                    current = await client.get(f"/api/terminals/{terminal_id}")
                    assert current.status_code == 200, current.text
                    if current.json()["data"]["release_reason"] == "pty_worker_error":
                        break
                    await asyncio.sleep(0.02)
            assert backend.poll() is None
        async with asyncio.timeout(8):
            while any(executing(process) for process in tracked):
                await asyncio.sleep(0.02)
        async with asyncio.timeout(8):
            while True:
                record = json.loads((root / ".boxteam/terminal-manager/terminals.json").read_text())["terminals"][0]
                if record["status"] == "exited":
                    break
                await asyncio.sleep(0.02)
        assert record["terminal_id"] == terminal_id
        assert record["os_pid"] == snapshot["os_pid"]
        assert record["process_session_id"] == snapshot["process_session_id"]
        assert record["process_start_time"] == snapshot["process_start_time"]
        assert record["transport_owner_revision"] == 4
        assert record["status"] == "exited" and record["reconciliation_status"] == "unknown"
        assert record["release_reason"] == ("pty_exit_cleanup_failed" if fail_sink else "pty_worker_error")
        assert record["args"] == ["-c", workload], "用户原生输入仍按原合同保留"
        attempts = [json.loads(line)["chunk"] for line in (control / "attempts.jsonl").read_text().splitlines()]
        assert len(attempts) == (2 if fail_sink else 1)
        assert "受控Worker I/O失败" in attempts[0] and str(control / "private-pty") in attempts[0]
        if fail_sink:
            diagnostics = "\n".join(file.read_text() for file in run.artifacts_dir.glob("terminal-backend-*.stderr.log"))
            assert "受控Worker I/O失败" in diagnostics and "ENOTDIR" in diagnostics
            # 独立重启只读原产品发布记录；不代填恢复状态或重发Worker故障。
            cold = start_terminal_backend(backend_port=int(url.rsplit(":", 1)[1]), workspace_root=str(root), artifacts_dir=run.artifacts_dir)
            try:
                current = await client.get(f"/api/terminals/{terminal_id}")
                assert current.status_code == 200, current.text
                assert all(current.json()["data"][key] == value for key, value in record.items())
            finally:
                terminate_process(cold)
        run.write_result("passed", {"fail_sink": fail_sink, "identities": identities,
                                   "scope": "真实HTTP/Node/PTY父子及Worker错误、日志writer、冷Manager读取；不代Gateway/Web、Windows或未知停止接管"})


@pytest.mark.parametrize("cold", [False, True], ids=["same-manager", "cold-manager"])
@pytest.mark.parametrize("action", ["kill", "delete"])
async def test_unknown_exit_without_worker_can_retry_management_with_original_process_identity(
    terminal_service: TerminalService, cold: bool, action: str,
) -> None:
    run, backend, url, workspace_id, root, control, tracked = terminal_service
    terminal_id = f"term_{uuid4().hex}"
    workload = f"""
import json, os, signal
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
child = os.fork()
if child == 0:
    while True: signal.pause()
Path({str(control / 'workload.pending')!r}).write_text(json.dumps({{'child_pid': child}}))
Path({str(control / 'workload.pending')!r}).replace({str(control / 'workload.json')!r})
while True: signal.pause()
"""
    headers = {"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-terminal"}
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=15, trust_env=False) as client:
        created = await client.post("/api/terminals", json={
            "terminal_id": terminal_id, "workspace_id": workspace_id, "owner_revision": 4,
            "command": sys.executable, "args": ["-c", workload],
        })
        assert created.status_code == 200, created.text
        original = created.json()["data"]
        root_process, worker = psutil.Process(original["os_pid"]), psutil.Process(original["pty_worker_pid"])
        tracked.extend([root_process, worker])
        async with asyncio.timeout(8):
            while not (control / "workload.json").is_file():
                await asyncio.sleep(0.02)
        child = psutil.Process(json.loads((control / "workload.json").read_text())["child_pid"])
        tracked.append(child)
        identities = [{"pid": process.pid, "created_at": process.create_time()} for process in tracked]
        (control / "fail-stop").touch()
        (control / "fail-worker").touch()
        state_file = root / ".boxteam/terminal-manager/terminals.json"
        async with asyncio.timeout(8):
            while True:
                record = json.loads(state_file.read_text())["terminals"][0]
                if record["status"] == "exited" and record["release_reason"] == "pty_exit_cleanup_failed" and not executing(worker):
                    break
                await asyncio.sleep(0.02)
        assert record["reconciliation_status"] == "unknown"
        assert executing(root_process) and executing(child)
        assert backend.poll() is None
        diagnostics = [json.loads(line)["chunk"] for line in (control / "attempts.jsonl").read_text().splitlines()]
        assert len(diagnostics) == 2
        assert "受控首次停止失败" in diagnostics[1] and "EPERM" in diagnostics[1]
        active_backend = backend
        if cold:
            terminate_process(backend)
            assert executing(root_process) and executing(child)
            active_backend = start_terminal_backend(backend_port=int(url.rsplit(":", 1)[1]), workspace_root=str(root), artifacts_dir=run.artifacts_dir)
        try:
            observed = await client.get(f"/api/terminals/{terminal_id}")
            assert observed.status_code == 200, observed.text
            unknown = observed.json()["data"]
            assert unknown["pty_worker_pid"] is None
            assert unknown["status"] == "exited" and unknown["reconciliation_status"] == "unknown"
            assert all(unknown[key] == original[key] for key in ("os_pid", "process_session_id", "process_start_time", "created_at", "transport_owner_revision"))
            stopped = await (client.post(f"/api/terminals/{terminal_id}/kill", json={}) if action == "kill"
                             else client.delete(f"/api/terminals/{terminal_id}"))
            assert stopped.status_code == 200, stopped.text
            if action == "kill":
                assert stopped.json()["data"]["killed"] is True
            else:
                assert stopped.json()["data"]["deleted"] is True
            snapshot = stopped.json()["data"]["terminal"]
            assert snapshot["status"] == ("terminated" if action == "kill" else "deleted")
            assert snapshot["reconciliation_status"] == "verified"
            assert snapshot["os_pid"] == original["os_pid"] and snapshot["transport_owner_revision"] == 4
            assert all(not executing(process) for process in tracked)
            record = json.loads(state_file.read_text())["terminals"][0]
            assert all(snapshot[key] == value for key, value in record.items())
            assert len((control / "starts.jsonl").read_text().splitlines()) == 1
            # 已核实终态重复请求不再触及任何进程，也不创建新执行。
            repeated = await (client.post(f"/api/terminals/{terminal_id}/kill", json={}) if action == "kill"
                              else client.delete(f"/api/terminals/{terminal_id}"))
            assert repeated.status_code == 200, repeated.text
            if action == "kill":
                assert repeated.json()["data"]["killed"] is False
            assert len((control / "starts.jsonl").read_text().splitlines()) == 1
            run.write_result("passed", {"cold": cold, "action": action, "identities": identities,
                                       "scope": "原HTTP管理重试、真实PTY父子停止及冷Manager原身份核验；不代Python lease、Gateway/Web、Windows或无PID接管"})
        finally:
            if cold:
                terminate_process(active_backend)
