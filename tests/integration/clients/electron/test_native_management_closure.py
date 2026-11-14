"""真实 Electron 管理来源保存、正常关窗及同 Home 重启恢复。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.electron.test_packaged_workbench import _run_process
from tests.integration.clients.electron.test_packaged_workbench import (
    electron_service_ports as electron_service_ports,  # noqa: PLC0414
)
from tests.integration.clients.electron.test_packaged_workbench import (
    packaged_electron as packaged_electron,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(autouse=True)
def management_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_NATIVE_MANAGEMENT_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_packaged_electron_management_save_and_restart(
    request: pytest.FixtureRequest,
    packaged_electron: Path,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
) -> None:
    client, context, workspace, _replay = metadata_case
    node = shutil.which("node")
    assert node, "原生 Electron inspector 需要目标 Node"
    response = await client.get("/api/gateway/workspaces")
    response.raise_for_status()
    envelope = response.json()
    assert envelope["request_id"] == response.headers["X-Request-ID"]
    owner = envelope["data"]["active_workspace"]
    credential = await client.get("/api/gateway/auth/local-credential")
    credential.raise_for_status()
    runtime, artifacts = context.runtime_root_for_node(request.node.nodeid), context.artifacts_dir
    command = [sys.executable, str(context.project_root / "app/runtime/posix_job.py"),
        "--memory-limit-kb", "4194304", "--timeout-seconds", "240", "--", "xvfb-run", "-a",
        node, "tests/integration/clients/electron/native_management_closure.mjs"]
    with chromium_temporary_environment(runtime / "native-tmp") as temporary:
        status = await asyncio.to_thread(_run_process, command, cwd=context.project_root,
            log=artifacts / "native-management-driver.log", timeout=255, environment={**os.environ, **temporary,
                "BOXTEAM_ELECTRON_EXECUTABLE": str(packaged_electron),
                "BOXTEAM_ELECTRON_GATEWAY": str(client.base_url).rstrip("/"),
                "BOXTEAM_ELECTRON_OWNER": json.dumps(owner),
                "BOXTEAM_ELECTRON_TOKEN": credential.json()["data"]["token"],
                "BOXTEAM_ELECTRON_RUNTIME": str(runtime), "BOXTEAM_ELECTRON_ARTIFACTS": str(artifacts),
                "BOXTEAM_NATIVE_MANAGEMENT_WORKSPACE": str(workspace),
            })
    assert status == 0, f"原生管理保存／重启失败：{artifacts}"
    result = json.loads((artifacts / "native-management.json").read_text())
    assert result["phase"] == "complete" and result["recoveredOwner"] == owner
    native = result["native"]
    assert len(native["launches"]) == len(native["exits"]) == 2
    assert all(exit_status["code"] == 0 and exit_status["signal"] is None for exit_status in native["exits"])
    assert native["launches"][0]["location"] == native["launches"][1]["location"]
    assert all(value["requestId"] for value in result["backendValues"])
    # 工作区观察在订阅变化时也会主动中止旧流；插件流只允许正常关窗取消。
    assert all(error.get("failure") == "net::ERR_ABORTED"
        and (error.get("request", "").endswith("/observations/stream")
            or (error.get("closing") and error.get("request", "").endswith(("/configuration-schemas/events", "/commands/events"))))
        for error in result["pageErrors"])
