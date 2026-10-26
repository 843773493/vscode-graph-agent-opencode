"""真实 stdio 强制关闭期限与停止旧实例后的 OS 退出恢复。"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import httpx
import portalocker
import psutil
import pytest

from configs.runtime import read_jsonc_object
from tests.integration.backend.sessions.test_mcp_config_recovery import (
    RecoveryCase,
    ready,
)
from tests.integration.backend.sessions.test_mcp_config_recovery import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 复用真实后端、持久事实及 OS 故障入口
)
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


@dataclass(frozen=True)
class StdioCase:
    backend: RecoveryCase
    lock: Path
    lifecycle: Path
    receipts: Path
    definitions: Path

    def configure(self, version: str, *, deadline: int = 10, noncooperative: bool = False) -> None:
        root = self.backend.mount.workspace_root
        path = root / ".boxteam/workspace.jsonc"
        payload = read_jsonc_object(path) if path.exists() else {}
        payload["mcp"] = {
            "supervision": {"sealed_call_timeout_seconds": deadline},
            "servers": {"schema": {
                "enabled": True, "transport": "stdio", "command": sys.executable,
                "args": list(map(str, (
                    Path.cwd() / "tests/integration/workspace_services/mcp/single_instance_server.py",
                    self.lock, self.lifecycle, self.definitions, self.receipts,
                    root / ".boxteam/state/workspace.sqlite",
                ))),
                "env": {"PYTHONPATH": str(Path.cwd()), "MCP_TEST_VERSION": version,
                        "MCP_TEST_SHUTDOWN": "noncooperative" if noncooperative else "normal"},
            }},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def phases(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.lifecycle.read_text().splitlines()]

    def client(self, base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base_url, timeout=10, headers={
            **LOCAL_TOKEN_HEADERS, "X-BoxTeam-Workspace-Id": self.backend.mount.workspace_id,
        })


@pytest.fixture
def stdio_case(recovery_case: RecoveryCase) -> StdioCase:
    artifacts = recovery_case.context.artifacts_dir
    case = StdioCase(recovery_case, *(artifacts / name for name in (
        "single.lock", "lifecycle.jsonl", "receipts.jsonl", "definitions.json",
    )))
    case.definitions.write_text(json.dumps({"probe": {"type": "object"}}), encoding="utf-8")
    return case


@pytest.mark.skipif(sys.platform == "win32", reason="验证 POSIX SDK 的 SIGTERM → SIGKILL；Windows Job Object 另行验收")
@pytest.mark.parametrize("deadline", [1, 10])
async def test_noncooperative_stdio_obeys_close_deadline_without_overlapping_instances(
    stdio_case: StdioCase, deadline: int,
) -> None:
    case, backend = stdio_case, stdio_case.backend
    case.configure("old", deadline=deadline, noncooperative=True)
    async with backend.process("recover") as (process, reports, base_url):
        original = await ready(process, reports)
        old_pid = case.phases()[0]["pid"]
        assert isinstance(old_pid, int)
        with pytest.raises(portalocker.exceptions.LockException), portalocker.Lock(case.lock, timeout=0):
            pytest.fail("旧实例没有持有真实单实例锁")
        started = monotonic()
        case.configure("new", deadline=deadline)
        async with case.client(base_url) as client:
            async with asyncio.timeout(15):
                while True:
                    response = await client.get("/api/v1/config/reload-status")
                    assert response.status_code == 200, response.text
                    status = response.json()["data"]
                    if status["state"] == ("recovery_required" if deadline == 1 else "active") and (
                        deadline == 1 or status["active_revision"] != original["active_revision"]
                    ):
                        break
                    await asyncio.sleep(0.05)
            elapsed = monotonic() - started
            if deadline == 1:
                assert not status["healthy"] and status["active_revision"] == original["active_revision"]
                assert status["applied_paths"] == [] and elapsed < 4
                unavailable = await client.get("/api/v1/mcp/servers", headers={"X-Request-ID": "stdio-unavailable-proof"})
                assert unavailable.status_code == 200, unavailable.text
                assert unavailable.headers["x-request-id"] == unavailable.json()["request_id"] == "stdio-unavailable-proof"
                server, = unavailable.json()["data"]
                assert server["enabled"] and server["status"] == "failed"
                assert server["tools"][0]["tool_id"] == "mcp__schema__probe"
                assert [(row["phase"], row["version"]) for row in case.phases()] == [("start", "old"), ("eof", "old")]
                assert psutil.pid_exists(old_pid)
                with pytest.raises(portalocker.exceptions.LockException), portalocker.Lock(case.lock, timeout=0):
                    pytest.fail("超时后真实关闭尚未结束，不能释放单实例锁")
                failed = backend.facts()
                assert json.loads(str(failed["config_active_snapshot"][0]["payload_json"]))["mcp"]["servers"]["schema"]["env"]["MCP_TEST_VERSION"] == "old"
                journal, = failed["config_apply_journal"]
                assert journal["state"] == "recovery_required"
                assert not any(event["result"] == "applied" for event in failed["config_events"])
                async with asyncio.timeout(8):
                    while psutil.pid_exists(old_pid):
                        await asyncio.sleep(0.05)
                with portalocker.Lock(case.lock, timeout=0):
                    pass
                # 原 scope 继续完成 SDK 关闭；配置修正后由原 watcher 重验，不能自动伪报成功。
                assert not any(row["phase"] == "start" and row["version"] == "new" for row in case.phases())
                case.configure("fixed", deadline=deadline)
                async with asyncio.timeout(15):
                    while True:
                        response = await client.get("/api/v1/config/reload-status")
                        status = response.json()["data"]
                        if status["healthy"] and status["active_revision"] != original["active_revision"]:
                            break
                        await asyncio.sleep(0.05)
                assert next(row for row in backend.facts()["config_apply_journal"] if row["apply_id"] == journal["apply_id"])["state"] == "recovery_required"
            else:
                assert status["healthy"] and elapsed < deadline
            assert not psutil.pid_exists(old_pid)
            phases = case.phases()
            assert [(row["phase"], row["version"]) for row in phases] == [
                ("start", "old"), ("eof", "old"), ("sigterm", "old"),
                ("start", "fixed" if deadline == 1 else "new"),
            ]
            assert phases[-1]["pid"] != old_pid
            assert phases[1]["effects"] == [{"resource": "mcp_connection", "action": "stop", "server_id": "schema",
                                              "connection_ref": original["connection_ref"]}]
            servers = await client.get("/api/v1/mcp/servers")
            assert servers.status_code == 200 and servers.json()["data"][0]["status"] == "ready", servers.text
            backend.context.artifacts_dir.joinpath(f"noncooperative-{deadline}.json").write_text(json.dumps({
                "elapsed": elapsed, "status": status, "phases": phases, "facts": backend.facts(),
            }, indent=2))
    with portalocker.Lock(case.lock, timeout=0):
        pass
    assert not case.receipts.exists(), "关闭和重验不得重放业务工具"


async def test_process_exit_after_stdio_stop_rebuilds_active_then_revalidates_candidate(stdio_case: StdioCase) -> None:
    case, backend = stdio_case, stdio_case.backend
    case.configure("old")
    async with backend.process("after_stdio_stop") as (process, reports, _):
        original = await ready(process, reports)
        case.configure("new")
        assert await asyncio.wait_for(asyncio.to_thread(process.wait), 20) == 83, (reports / "process.raw").read_text()
        fault = json.loads((reports / "fault.json").read_text())
        assert fault["server"]["env"]["MCP_TEST_VERSION"] == "old"
        assert fault["connection_ref"] == original["connection_ref"]
        assert fault["state"] == "applying" and fault["catalog_status"] == "unavailable"
    crashed = backend.facts()
    journal, = crashed["config_apply_journal"]
    pending, = crashed["config_pending_candidate"]
    claim, = crashed["config_apply_claim"]
    assert journal["state"] == pending["state"] == "applying"
    assert json.loads(str(crashed["config_active_snapshot"][0]["payload_json"]))["mcp"]["servers"]["schema"]["env"]["MCP_TEST_VERSION"] == "old"
    assert json.loads(str(journal["side_effects_json"])) == [{
        "resource": "mcp_connection", "action": "stop", "server_id": "schema", "connection_ref": original["connection_ref"],
    }]
    assert not crashed["config_events"]
    assert [(row["phase"], row["version"]) for row in case.phases()] == [("start", "old"), ("stop", "old")]
    old_pid = case.phases()[0]["pid"]
    assert isinstance(old_pid, int) and not psutil.pid_exists(old_pid)
    with portalocker.Lock(case.lock, timeout=0):
        pass
    async with backend.process("recover") as (process, reports, base_url):
        restored = await ready(process, reports)
        expiry = datetime.fromisoformat(str(claim["lease_expires_at"]))
        assert datetime.now(UTC) < expiry, "未覆盖 claim 仍有效的 stdio 冷重建窗口"
        server = restored["server"]
        assert isinstance(server, dict) and server["env"]["MCP_TEST_VERSION"] == "old"
        assert restored["connection_ref"] != original["connection_ref"]
        assert restored["state"] == "applying" and not restored["healthy"]
        async with case.client(base_url) as client, asyncio.timeout(40):
            while True:
                response = await client.get("/api/v1/config/reload-status")
                assert response.status_code == 200, response.text
                status = response.json()["data"]
                if datetime.now(UTC) < expiry:
                    live, = backend.facts()["config_apply_claim"]
                    assert live["fencing_token"] == claim["fencing_token"]
                    assert status["active_revision"] == original["active_revision"]
                if status["healthy"] and not backend.facts()["config_apply_claim"]:
                    break
                await asyncio.sleep(0.1)
            assert status["applied_paths"] == ["/mcp/servers/schema/env/MCP_TEST_VERSION"]
        recovered = backend.facts()
        assert next(row for row in recovered["config_apply_journal"] if row["apply_id"] == journal["apply_id"])["state"] == "recovery_required"
        assert [event["result"] for event in recovered["config_events"]].count("applied") == 1
        assert json.loads(str(recovered["config_active_snapshot"][0]["payload_json"]))["mcp"]["servers"]["schema"]["env"]["MCP_TEST_VERSION"] == "new"
        assert [(row["phase"], row["version"]) for row in case.phases()] == [
            ("start", "old"), ("stop", "old"), ("start", "old"), ("stop", "old"), ("start", "new"),
        ]
        assert len({row["pid"] for row in case.phases() if row["phase"] == "start"}) == 3
        backend.context.artifacts_dir.joinpath("stop-window.json").write_text(json.dumps({
            "crashed": crashed, "restored": restored, "recovered": recovered, "phases": case.phases(),
        }, indent=2))
    with portalocker.Lock(case.lock, timeout=0):
        pass
    assert not case.receipts.exists(), "OS 退出恢复不得重放业务工具"
