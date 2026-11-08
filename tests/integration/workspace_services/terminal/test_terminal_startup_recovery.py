"""迟到创建丢失停止证明后，同进程及硬重启的原管理入口核实原生身份。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.harness.python.terminal_backend import start_terminal_plugin_service
from tests.integration.workspace_services.terminal.test_terminal_startup_cleanup import (
    TerminalService,
    is_executing,
    wait_observed,
    workload,
)
from tests.integration.workspace_services.terminal.test_terminal_startup_cleanup import (
    terminal_service as terminal_service,  # noqa: PLC0414 - 复用真实服务与精确身份收尾。
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="真实Linux PTY身份和硬重启；不代替Windows")


@pytest.mark.parametrize("cold", [False, True], ids=["same-manager", "cold-manager"])
@pytest.mark.parametrize("action", ["kill", "delete"])
async def test_late_creation_without_stop_proof_can_reconcile_original_identity(
    terminal_service: TerminalService, monkeypatch: pytest.MonkeyPatch, cold: bool, action: str,
) -> None:
    service = terminal_service
    terminal_id = f"term_{uuid4().hex}"
    for marker in ["late-native-identity", "drop-stop-proof"]:
        (service.control / marker).touch()
    (service.control / "mode").write_text("late-native-identity")
    headers = service.process.headers
    state_file = service.root / ".boxteam/terminal-manager/terminals.json"
    async with httpx.AsyncClient(base_url=service.url, headers=headers, timeout=15, trust_env=False) as client:
        creation = await client.post("/api/terminals", json={
            "terminal_id": terminal_id, "workspace_id": service.workspace_id, "owner_revision": 7,
            "command": sys.executable, "args": ["-c", workload(service.control, ignore_sigterm=False)],
        })
        assert creation.status_code == 500, creation.text
        assert "creation_receipt" not in creation.json()
        observed = await wait_observed(service.control)
        assert all(not is_executing(identity) for identity in observed.values())
        original, = json.loads(state_file.read_text())["terminals"]
        assert original["status"] == "created" and original["reconciliation_status"] == "unknown"
        assert original["os_pid"] == observed["root"]["pid"]
        for field, native in [("process_group_id", "processGroupId"),
                              ("process_session_id", "processSessionId"),
                              ("process_start_time", "processStartTime")]:
            assert original[field] == observed["root"][native]
        original_workload = (service.control / "workload.json").read_bytes()
        if cold:
            # 只杀本fixture拥有的Node后端；不替产品写恢复记录、补元数据或核实终态。
            service.process.process.kill()
            assert await asyncio.to_thread(service.process.process.wait, timeout=5) == -9
            with monkeypatch.context() as env:
                env.setenv("NODE_OPTIONS", f"--import={(Path.cwd() / 'tests/integration/workspace_services/terminal/terminal_startup_failure.mjs').as_uri()}")
                env.setenv("BOXTEAM_TEST_PROJECT_ROOT", str(Path.cwd()))
                env.setenv("BOXTEAM_TEST_TERMINAL_CONTROL", str(service.control))
                service.process = start_terminal_plugin_service(
                    backend_port=int(service.process.root_url.rsplit(":", 1)[1]), workspace_root=str(service.root),
                    artifacts_dir=service.run.artifacts_dir / "restarted",
                )
            client.headers.update(service.process.headers)
        current = await client.get(f"/api/terminals/{terminal_id}")
        assert current.status_code == 200, current.text
        unknown = current.json()["data"]
        assert unknown["status"] == "created" and unknown["reconciliation_status"] == "unknown"
        assert unknown["pty_worker_pid"] is None
        for field in ("os_pid", "process_group_id", "process_session_id", "process_start_time",
                      "created_at", "transport_owner_revision"):
            assert unknown[field] == original[field]
        final = unknown
        for attempt in range(2):
            response = await (client.post(f"/api/terminals/{terminal_id}/kill", json={}) if action == "kill"
                              else client.delete(f"/api/terminals/{terminal_id}"))
            assert response.status_code == 200, response.text
            if action == "kill":
                assert response.json()["data"]["killed"] is (attempt == 0)
            else:
                assert response.json()["data"]["deleted"] is True
            final = response.json()["data"]["terminal"]
            assert final["status"] == ("terminated" if action == "kill" else "deleted")
            assert final["reconciliation_status"] == "verified" and final["pty_worker_pid"] is None
            for field in ("os_pid", "process_group_id", "process_session_id", "process_start_time",
                          "created_at", "transport_owner_revision"):
                assert final[field] == original[field]
            record, = json.loads(state_file.read_text())["terminals"]
            assert all(final[key] == value for key, value in record.items())
        assert (service.control / "workload.json").read_bytes() == original_workload
        assert all(not is_executing(identity) for identity in observed.values())
        receipts, = (service.control / "stop-receipts.jsonl").read_text().splitlines()
        assert json.loads(receipts)["pid"] == original["os_pid"]
        service.run.write_result("passed", {"cold": cold, "action": action, "observed": observed,
                                           "unknown": unknown, "final": final,
                                           "scope": "真实Linux HTTP/PTY、丢回执及Node硬重启；不代Python lease、无原生身份接管、Windows或Gateway/Web"})
