"""真实PTY就绪尚未交付时硬退出，冷Manager必须保留原创建意图。"""

from __future__ import annotations

import asyncio
import json
import sys
from uuid import uuid4

import httpx
import pytest

from tests.harness.python.terminal_backend import (
    start_terminal_plugin_service,
    stop_terminal_plugin_service,
)
from tests.integration.workspace_services.terminal.test_terminal_shutdown_signals import (
    TerminalService,
    wait_file,
)
from tests.integration.workspace_services.terminal.test_terminal_shutdown_signals import (
    terminal_service as terminal_service,  # noqa: PLC0414 - 复用原真实PTY屏障与精确收尾。
)
from tests.integration.workspace_services.terminal.test_terminal_startup_cleanup import (
    is_executing,
    process_identity,
    workload,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="真实Linux PTY与Node硬退出；不代替Windows")


async def test_native_creation_before_ready_keeps_cold_unknown_intent(terminal_service: TerminalService) -> None:
    run, plugin, url, workspace_id, control, root = terminal_service
    terminal_id = f"term_{uuid4().hex}"
    headers = plugin.headers
    payload = {"terminal_id": terminal_id, "workspace_id": workspace_id, "owner_revision": 7,
               "command": sys.executable, "args": ["-c", workload(control, ignore_sigterm=False)]}
    state_file = root / ".boxteam/terminal-manager/terminals.json"
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=15, trust_env=False) as client:
        creating = asyncio.create_task(client.post("/api/terminals", json=payload))
        restarted = None
        try:
            native = json.loads(await wait_file(control / "arrived.json"))
            workload_bytes = (await wait_file(control / "workload.json")).encode()
            child = process_identity(json.loads(workload_bytes)["child_pid"])
            worker = process_identity(native["worker_pid"])
            assert child is not None and worker is not None
            assert all(is_executing(identity) for identity in (native, child, worker))
            record, = json.loads(state_file.read_bytes())["terminals"]
            assert record["terminal_id"] == terminal_id and record["transport_owner_revision"] == 7
            assert record["status"] == "created" and record["reconciliation_status"] == "unknown"
            for field in ("os_pid", "process_group_id", "process_session_id", "process_start_time", "started_at"):
                assert record[field] is None
            assert not creating.done()
            plugin.process.kill()
            assert await asyncio.to_thread(plugin.process.wait, timeout=5) == -9
            with pytest.raises(httpx.TransportError):
                await creating
            # 复用原磁盘启动真正的新Manager，不补写意图、身份或停止证明。
            restarted = await asyncio.to_thread(
                start_terminal_plugin_service, backend_port=int(plugin.root_url.rsplit(":", 1)[1]), workspace_root=str(root),
                artifacts_dir=run.artifacts_dir / "restarted",
            )
            client.headers.update(restarted.headers)
            response = await client.get(f"/api/terminals/{terminal_id}")
            assert response.status_code == 200, response.text
            cold = response.json()["data"]
            for field, value in record.items():
                assert cold[field] == value
            assert cold["pty_worker_pid"] is None
            # 缺少原生身份仍不能假报停止；已有intent也不能被迟到创建替换。
            response = await client.post(f"/api/terminals/{terminal_id}/kill", json={})
            assert response.status_code == 200, response.text
            assert response.json()["data"]["killed"] is False
            unknown = response.json()["data"]["terminal"]
            assert unknown["status"] == "created" and unknown["reconciliation_status"] == "unknown"
            response = await client.delete(f"/api/terminals/{terminal_id}")
            assert response.status_code == 500, response.text
            response = await client.post("/api/terminals", json=payload)
            assert response.status_code == 500 and "creation_receipt" not in response.json(), response.text
            final, = json.loads(state_file.read_bytes())["terminals"]
            for field in ("terminal_id", "transport_owner_revision", "status", "reconciliation_status", "created_at",
                          "os_pid", "process_group_id", "process_session_id", "process_start_time"):
                assert final[field] == record[field]
            assert (control / "workload.json").read_bytes() == workload_bytes
            assert len((control / "starts.jsonl").read_text().splitlines()) == 1
            run.write_result("passed", {"native": native, "child": child, "worker": worker,
                                        "before_crash": record, "cold": cold, "final": final,
                                        "scope": "真实HTTP/PTY与Node硬退出后的可发现unknown；不证明无原生身份接管、Python lease、Windows或Web"})
        finally:
            if not creating.done():
                creating.cancel()
                await asyncio.gather(creating, return_exceptions=True)
            if restarted is not None:
                await asyncio.to_thread(stop_terminal_plugin_service, restarted)
