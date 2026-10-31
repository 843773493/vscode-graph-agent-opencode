"""原PTY根退出后，显式管理沿原SID核实后代，不重新启动终端。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import pytest

from tests.harness.python.terminal_backend import start_terminal_backend
from tests.integration.workspace_services.terminal.test_terminal_exit_diagnostics import (
    TerminalService,
    executing,
)
from tests.integration.workspace_services.terminal.test_terminal_exit_diagnostics import (
    terminal_service as terminal_service,  # noqa: PLC0414 - 复用真实PTY及精确进程身份收尾。
)
from tests.support.processes.workspace import terminate_process

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="真实Linux SID与/proc；不代替其它平台")


@pytest.mark.parametrize("cold", [False, True], ids=["same-manager", "cold-manager"])
@pytest.mark.parametrize("action", ["kill", "delete"])
async def test_missing_pty_root_still_verifies_original_session_descendants(
    terminal_service: TerminalService, cold: bool, action: str,
) -> None:
    run, backend, url, workspace_id, root, control, tracked = terminal_service
    terminal_id = f"term_{uuid4().hex}"
    workload = f"""
import json, os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
child = os.fork()
if child == 0:
    while True: signal.pause()
Path({str(control / 'workload.pending')!r}).write_text(json.dumps({{'child_pid': child}}))
Path({str(control / 'workload.pending')!r}).replace({str(control / 'workload.json')!r})
while not Path({str(control / 'exit-root')!r}).exists(): time.sleep(0.02)
os._exit(0)
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
        assert os.getsid(child.pid) == original["process_session_id"]
        (control / "fail-stop").touch()
        (control / "fail-worker").touch()
        state_file = root / ".boxteam/terminal-manager/terminals.json"
        async with asyncio.timeout(8):
            while True:
                record = json.loads(state_file.read_text())["terminals"][0]
                if record["status"] == "exited" and record["reconciliation_status"] == "unknown" and not executing(worker):
                    break
                await asyncio.sleep(0.02)
        assert executing(root_process) and executing(child)
        # 原命令自行退出根进程，保留真实后代；测试不写资源状态或补填恢复身份。
        (control / "exit-root").touch()
        async with asyncio.timeout(8):
            while Path(f"/proc/{root_process.pid}").exists():
                await asyncio.sleep(0.02)
        assert executing(child) and os.getsid(child.pid) == original["process_session_id"]
        before = state_file.read_bytes()
        active_backend = backend
        if cold:
            terminate_process(backend)
            active_backend = start_terminal_backend(
                backend_port=int(url.rsplit(":", 1)[1]), workspace_root=str(root), artifacts_dir=run.artifacts_dir,
            )
        try:
            observed = await client.get(f"/api/terminals/{terminal_id}")
            assert observed.status_code == 200, observed.text
            unknown = observed.json()["data"]
            assert unknown["pty_worker_pid"] is None and unknown["reconciliation_status"] == "unknown"
            assert unknown["status"] == "exited" and executing(child)
            for key in ("os_pid", "process_group_id", "process_session_id", "process_start_time", "created_at", "transport_owner_revision"):
                assert unknown[key] == original[key]
            stopped = await (client.post(f"/api/terminals/{terminal_id}/kill", json={}) if action == "kill"
                             else client.delete(f"/api/terminals/{terminal_id}"))
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["data"]["killed" if action == "kill" else "deleted"] is True
            final = stopped.json()["data"]["terminal"]
            assert final["status"] == ("terminated" if action == "kill" else "deleted")
            assert final["reconciliation_status"] == "verified" and final["pty_worker_pid"] is None
            assert final["os_pid"] == original["os_pid"] and final["transport_owner_revision"] == 4
            assert all(not executing(process) for process in tracked)
            final_record = json.loads(state_file.read_text())["terminals"][0]
            assert all(final[key] == value for key, value in final_record.items())
            assert len((control / "starts.jsonl").read_text().splitlines()) == 1
            repeated = await (client.post(f"/api/terminals/{terminal_id}/kill", json={}) if action == "kill"
                              else client.delete(f"/api/terminals/{terminal_id}"))
            assert repeated.status_code == 200, repeated.text
            if action == "kill":
                assert repeated.json()["data"]["killed"] is False
            assert state_file.read_bytes() != before
            run.write_result("passed", {"cold": cold, "action": action, "identities": identities,
                                       "before": unknown, "after": final,
                                       "scope": "Linux真实PTY根缺失及原SID后代停止；不代Windows、Python lease或无PID创建接管"})
        finally:
            if cold:
                terminate_process(active_backend)
