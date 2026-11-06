"""真实 Linux ASAR 复用规范文件身份场景，恢复边界使用正常关窗及同 profile 重启。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.electron.test_packaged_workbench import (
    _run_process,
)
from tests.integration.clients.electron.test_packaged_workbench import (
    electron_service_ports as electron_service_ports,  # noqa: PLC0414
)
from tests.integration.clients.electron.test_packaged_workbench import (
    packaged_electron as packaged_electron,  # noqa: PLC0414
)
from tests.integration.clients.web.quick_access.test_file_canonical import (
    assert_canonical_result,
)
from tests.integration.clients.web.quick_access.test_file_canonical import (
    canonical_workspaces as canonical_workspaces,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import TestFilePortBlock
from tests.support.processes.gateway import close_gateway_process, start_gateway_process


@pytest.mark.asyncio
async def test_packaged_electron_canonical_dirty_backup_and_conflict(
    request: pytest.FixtureRequest,
    canonical_workspaces: tuple[TestRunContext, list[Path]],
    packaged_electron: Path,
    electron_service_ports: TestFilePortBlock,
) -> None:
    context, workspaces = canonical_workspaces
    runtime = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir
    result_path = artifacts / "file-canonical.json"
    node = shutil.which("node")
    assert node is not None, "Playwright 原生 Electron inspector 驱动需要 Node"
    gateway = await asyncio.to_thread(start_gateway_process,
        workspace_root=workspaces[0], default_backend_url=None, port=electron_service_ports.port(20),
        boxteam_home=runtime / "server-home", artifacts_dir=artifacts / "gateway",
        extra_env={"LITELLM_LOCAL_MODEL_COST_MAP": "true"})
    supervisor = context.project_root / "app/runtime/posix_job.py"
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "300", "--",
               "xvfb-run", "-a", node, "tests/integration/clients/electron/file_canonical.mjs"]
    try:
        with chromium_temporary_environment(runtime / "native-tmp") as temporary:
            exit_code = await asyncio.to_thread(_run_process, command, cwd=context.project_root,
                log=artifacts / "driver.log", timeout=315, environment={**os.environ, **temporary,
                    "BOXTEAM_ELECTRON_EXECUTABLE": str(packaged_electron),
                    "BOXTEAM_ELECTRON_GATEWAY": f"http://127.0.0.1:{gateway.port}",
                    "BOXTEAM_ELECTRON_RUNTIME": str(runtime), "BOXTEAM_ELECTRON_ARTIFACTS": str(artifacts),
                    "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                    "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(artifacts / "file-canonical-failure.png"),
                    "BOXTEAM_CANONICAL_WORKSPACE": str(workspaces[0]), "BOXTEAM_CANONICAL_OTHER": str(workspaces[1])})
        assert exit_code == 0, f"Electron 规范文件身份验收失败：{artifacts}"
        result = assert_canonical_result(result_path, workspaces)
        native = result["native"]
        assert isinstance(native, dict)
        assert len(native["launches"]) == 3 and len(native["exits"]) == 3
        assert len({entry["url"] for entry in native["launches"]}) == 1
        assert all(entry["code"] == 0 and entry["signal"] is None for entry in native["exits"])
        assert not [entry for entry in native["consoleErrors"] if "IndexedDB" in entry["text"]
                    or "database" in entry["text"].lower() and "closed" in entry["text"].lower()]
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
