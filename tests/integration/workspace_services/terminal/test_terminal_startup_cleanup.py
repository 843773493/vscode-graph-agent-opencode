"""原生创建后、就绪前失败的真实PTY停止证明与原管理入口重试。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.processes.workspace import terminate_process
from tests.support.workspaces import prepare_default_test_workspace

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="核对Linux原生PTY与/proc，不替代Windows验收")


def process_identity(pid: int) -> dict[str, int | str] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = raw.rsplit(") ", 1)[1].split()
    return {"pid": pid, "state": fields[0], "processSessionId": int(fields[3]), "processStartTime": int(fields[19])}


def is_executing(identity: dict[str, int | str]) -> bool:
    current = process_identity(int(identity["pid"]))
    return current is not None and current["processStartTime"] == identity["processStartTime"] and current["state"] not in {"Z", "X", "x"}


@dataclass
class TerminalService:
    run: TestRunContext
    url: str
    workspace_id: str
    control: Path
    root: Path


@pytest.fixture
def terminal_service(integration_backend_port: int, monkeypatch: pytest.MonkeyPatch) -> Iterator[TerminalService]:
    with TestRunContext.from_test_file(Path(__file__)) as run:
        root = prepare_default_test_workspace(
            workspace_root=run.workspace_root / uuid4().hex,
            template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        )
        control = run.artifacts_dir / "control"
        control.mkdir(parents=True)
        with monkeypatch.context() as env:
            env.setenv("NODE_OPTIONS", f"--import={(Path.cwd() / 'tests/integration/workspace_services/terminal/terminal_startup_failure.mjs').as_uri()}")
            env.setenv("BOXTEAM_TEST_PROJECT_ROOT", str(Path.cwd()))
            env.setenv("BOXTEAM_TEST_TERMINAL_CONTROL", str(control))
            process = start_terminal_backend(
                backend_port=integration_backend_port + 67, workspace_root=str(root), artifacts_dir=run.artifacts_dir,
            )
        try:
            yield TerminalService(run, f"http://127.0.0.1:{integration_backend_port + 67}", load_or_create_workspace_id(root), control, root)
        finally:
            try:
                terminate_process(process)
            finally:
                # 失败收尾只停止本fixture观察到的精确实例，不替产品签回执或修改恢复状态。
                identities: list[dict[str, int | str]] = []
                if (control / "starts.jsonl").exists():
                    identities.extend(json.loads(line) for line in (control / "starts.jsonl").read_text().splitlines())
                if (control / "observed.json").exists():
                    identities.extend(json.loads((control / "observed.json").read_text()).values())
                for identity in identities:
                    if is_executing(identity):
                        os.kill(int(identity["pid"]), signal.SIGKILL)


async def wait_observed(control: Path) -> dict[str, dict[str, int | str]]:
    async with asyncio.timeout(8):
        while not (control / "observed.json").is_file():
            await asyncio.sleep(0.02)
    return json.loads((control / "observed.json").read_text())


def workload(control: Path) -> str:
    return f"""# gated-terminal-startup-cleanup
import json, os, signal
from pathlib import Path
control = Path({str(control)!r})
signal.signal(signal.SIGTERM, signal.SIG_IGN)
read_fd, write_fd = os.pipe()
child = os.fork()
if child == 0:
    os.close(read_fd)
    os.write(write_fd, b'ready')
    os.close(write_fd)
    while True:
        signal.pause()
os.close(write_fd)
assert os.read(read_fd, 5) == b'ready'
os.close(read_fd)
(control / 'workload.pending').write_text(json.dumps({{'pid': os.getpid(), 'child_pid': child}}))
(control / 'workload.pending').replace(control / 'workload.json')
while True:
    signal.pause()
"""


@pytest.mark.parametrize("mode", ["stop-succeeds", "stop-fails"])
async def test_startup_failure_requires_tree_and_worker_stop_before_receipt(terminal_service: TerminalService, mode: str) -> None:
    service = terminal_service
    terminal_id, peer_id = [f"term_{uuid4().hex}" for _ in range(2)]
    (service.control / "mode").write_text(mode)
    headers = {"X-BoxTeam-Workspace-Id": service.workspace_id, "X-BoxTeam-Resource-Owner": "workspace-terminal"}
    payload = {"workspace_id": service.workspace_id, "owner_revision": 7, "command": sys.executable}
    state_file = service.root / ".boxteam/terminal-manager/terminals.json"
    async with httpx.AsyncClient(base_url=service.url, headers=headers, timeout=15) as client:
        peer = await client.post("/api/terminals", json=payload | {
            "terminal_id": peer_id, "args": ["-c", "import time; time.sleep(300)"],
        })
        assert peer.status_code == 200, peer.text
        peer_snapshot = peer.json()["data"]
        creation = asyncio.create_task(client.post("/api/terminals", json=payload | {
            "terminal_id": terminal_id, "args": ["-c", workload(service.control)],
        }))
        try:
            observed = await wait_observed(service.control)
            root, child, worker = [observed[key] for key in ("root", "child", "worker")]
            assert root["processSessionId"] == root["pid"] == child["processSessionId"]
            assert worker["pid"] != root["pid"]
            failed = await creation
            assert failed.status_code == 500, failed.text
            current = await client.get(f"/api/terminals/{terminal_id}")
            assert current.status_code == 200, current.text
            snapshot = current.json()["data"]
            assert snapshot["os_pid"] == root["pid"]
            assert snapshot["transport_owner_revision"] == 7
            records = json.loads(state_file.read_text(encoding="utf-8"))["terminals"]
            record = next(record for record in records if record["terminal_id"] == terminal_id)
            assert record["os_pid"] == root["pid"]
            assert record["process_session_id"] == root["processSessionId"]
            if mode == "stop-fails":
                assert "creation_receipt" not in failed.json()
                assert snapshot["status"] == record["status"] == "created"
                assert snapshot["reconciliation_status"] == record["reconciliation_status"] == "unknown"
                assert snapshot["pty_worker_pid"] == worker["pid"]
                assert all(is_executing(identity) for identity in observed.values())
            else:
                assert failed.json()["creation_receipt"] == {
                    "resource_id": terminal_id, "workspace_id": service.workspace_id, "owner_revision": 7,
                    "outcome": "failed", "resource": snapshot,
                }
                assert snapshot["status"] == record["status"] == "terminated"
                assert snapshot["reconciliation_status"] == record["reconciliation_status"] == "verified"
                assert snapshot["pty_worker_pid"] is None
                assert all(not is_executing(identity) for identity in observed.values())
            # 未知创建不阻塞其他资源；管理重试必须沿原身份停止，而不是重新创建。
            peer_current = await client.get(f"/api/terminals/{peer_id}")
            assert peer_current.status_code == 200, peer_current.text
            assert peer_current.json()["data"]["os_pid"] == peer_snapshot["os_pid"]
            assert peer_current.json()["data"]["status"] == "running"
            if mode == "stop-fails":
                retried = await client.post(f"/api/terminals/{terminal_id}/kill", json={})
                assert retried.status_code == 200, retried.text
                snapshot = retried.json()["data"]["terminal"]
                assert retried.json()["data"]["killed"] is True
                assert snapshot["os_pid"] == root["pid"] and snapshot["transport_owner_revision"] == 7
                assert snapshot["status"] == "terminated" and snapshot["reconciliation_status"] == "verified"
                assert snapshot["pty_worker_pid"] is None
                assert all(not is_executing(identity) for identity in observed.values())
            records = json.loads(state_file.read_text(encoding="utf-8"))["terminals"]
            record = next(record for record in records if record["terminal_id"] == terminal_id)
            assert all(record[key] == snapshot[key] for key in record)
            assert (await client.post(f"/api/terminals/{peer_id}/kill", json={})).status_code == 200
            service.run.write_result("passed", {"mode": mode, "observed": observed, "scope": "Linux真实PTY/HTTP及持久记录；不代表Gateway/Web、Windows或未交付PID的native阻塞"})
        finally:
            if not creation.done():
                creation.cancel()
                await asyncio.gather(creation, return_exceptions=True)
