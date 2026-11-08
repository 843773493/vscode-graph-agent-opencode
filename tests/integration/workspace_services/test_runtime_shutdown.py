"""原辅助服务 HTTP 关闭入口：身份拒绝不停止服务，ACK 后由原 Manager 收尾。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import (
    TerminalPluginService,
)
from tests.integration.workspace_services.test_http_shutdown_deadline import (
    service as service,  # noqa: PLC0414
)
from tests.support.processes.workspace import ensure_local_port_available


@pytest.fixture(autouse=True)
def runtime_control_environment(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "workspace-service-shutdown-control"
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", token)
    configured = os.environ.get("BOXTEAM_TEST_NODE_HOST_EXECUTABLE")
    if configured:
        monkeypatch.setenv("PATH", str(Path(configured).parent) + os.pathsep + os.environ["PATH"])
    return token


async def test_authenticated_control_shuts_down_original_workspace_service(
    service: tuple[str, subprocess.Popen[str], int, str, Path, TestRunContext, TerminalPluginService | None],
    runtime_control_environment: str,
) -> None:
    kind, process, port, workspace_id, root, run, terminal_plugin = service
    base_url = f"http://127.0.0.1:{port}"
    headers = {"X-Local-Token": runtime_control_environment}
    route = "/internal/runtime/shutdown"
    if kind == "terminal":
        assert terminal_plugin is not None
        base_url = terminal_plugin.root_url
        headers = {"X-Local-Token": terminal_plugin.token}
        route = "/internal/plugin/shutdown"
    async with httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=3) as client:
        if kind == "terminal":
            before = (await client.get("/internal/plugin/status", headers=headers)).raise_for_status().json()
            assert before["process_id"] == process.pid and before["workspace_id"] == workspace_id
            payload = {}
        else:
            before = (await client.get("/health")).raise_for_status().json()
            assert before["process_id"] == process.pid and before["workspace_id"] == workspace_id
            payload = {"process_id": before["process_id"], "workspace_id": workspace_id}
        for sent in ({}, {"X-Local-Token": "wrong-token"}):
            assert (await client.post(route, json=payload, headers=sent)).status_code == 401
        for extra in ({"Origin": base_url}, {"Sec-Fetch-Site": "same-origin"}):
            assert (await client.post(route, json=payload, headers=headers | extra)).status_code == 403
        assert (await client.get(route, headers=headers)).status_code == 405
        assert (await client.post(route, json=payload | {"extra": True}, headers=headers)).status_code == 400
        if kind == "terminal":
            assert (await client.get("/internal/plugin/status", headers=headers)).raise_for_status().json() == before
        else:
            for changed in ({"process_id": process.pid + 1}, {"workspace_id": "other-workspace"}):
                assert (await client.post(route, json=payload | changed, headers=headers)).status_code == 409
            assert (await client.get("/health")).raise_for_status().json() == before
        assert process.poll() is None
        accepted = (await client.post(route, json=payload, headers=headers)).raise_for_status().json()
        if kind == "terminal":
            assert accepted == {"accepted": True, "process_id": process.pid, "instance_id": terminal_plugin.instance_id}
        else:
            assert accepted == {"data": payload | {"accepted": True}}
    assert await asyncio.to_thread(process.wait, 8) == 0
    ensure_local_port_available(port, service_name=f"已关闭的 {kind} 管理进程")
    state_file = root / f".boxteam/{kind}-manager/{'terminals' if kind == 'terminal' else f'{kind}s'}.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["terminals" if kind == "terminal" else f"{kind}s"] == []
    (run.artifacts_dir / f"runtime-shutdown-{kind}.json").write_text(json.dumps({
        "kind": kind, "identity": payload, "accepted": accepted, "exit_code": process.returncode,
        "scope": "真实 Node、HTTP、原 Manager 及进程退出；空资源集合，不代替有资源排空或完整 Gateway 验收",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
