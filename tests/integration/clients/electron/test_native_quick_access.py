"""真实 Linux Electron ASAR 中验证 Quick Access、正文范围与搜索编辑器重启恢复。"""

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
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import TestFilePortBlock
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(scope="module", autouse=True)
def native_search_service_ports(electron_service_ports: TestFilePortBlock) -> None:
    """先为打包及原生代理保留端口，再由 Gateway fixture 分配剩余端口。"""


async def test_packaged_electron_quick_access_scoped_search_and_command_center(
    request: pytest.FixtureRequest,
    packaged_electron: Path,
    electron_service_ports: TestFilePortBlock,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, object],
) -> None:
    client, context, workspace, _provider = metadata_case
    assert sys.platform == "linux"
    workspace.joinpath("native-quick-access.txt").write_text(
        "native quick access scope token\n", encoding="utf-8",
    )
    context.workspace_root.mkdir(parents=True, exist_ok=True)
    other = prepare_default_test_workspace(
        workspace_root=context.workspace_root / context.run_root.name / "native-quick-access-B",
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        fresh_identity=True,
    )
    install_test_workspace_config(
        workspace_root=other, config_path=context.project_root / "configs/tests/workspace/default.jsonc",
        schema_path=context.project_root / "configs/workspace_schema.jsonc",
    )
    (other / "native-quick-access.txt").write_text(
        "other native quick access scope token\n", encoding="utf-8",
    )
    runtime = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir
    result_path = artifacts / "native-quick-access.json"
    node = shutil.which("node")
    assert node is not None, "Playwright Electron inspector 需要 Node"
    async with httpx.AsyncClient(base_url=str(client.base_url), headers=client.headers, timeout=30) as api:
        navigation = await api.get("/api/gateway/workspaces")
        assert navigation.status_code == 200, navigation.text
        owner = navigation.json()["data"]["active_workspace"]
        assert owner["workspace_id"]
        registered = await api.post("/api/gateway/managed-workspaces", json={
            "root_path": str(other), "name": "原生范围 B",
        })
        assert registered.status_code in (200, 201), registered.text
        all_workspaces = await api.get("/api/gateway/workspaces?check_health=false")
        assert all_workspaces.status_code == 200, all_workspaces.text
        other_owner = next(item for item in all_workspaces.json()["data"]["items"] if item["name"] == "原生范围 B")
        assert other_owner["workspace_id"] != owner["workspace_id"]
    command = [sys.executable, str(context.project_root / "app/runtime/posix_job.py"),
        "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--", "xvfb-run", "-a",
        node, "tests/integration/clients/electron/native_quick_access.mjs"]
    with chromium_temporary_environment(runtime / "native-tmp") as temporary:
        environment = {**os.environ, **temporary,
            "BOXTEAM_ELECTRON_EXECUTABLE": str(packaged_electron),
            "BOXTEAM_ELECTRON_GATEWAY": str(client.base_url).rstrip("/"),
            "BOXTEAM_ELECTRON_PROXY_PORT": str(electron_service_ports.port(21)),
            "BOXTEAM_ELECTRON_RUNTIME": str(runtime),
            "BOXTEAM_ELECTRON_ARTIFACTS": str(artifacts),
            "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
            "BOXTEAM_NATIVE_SEARCH_OWNER": json.dumps(owner),
            "BOXTEAM_NATIVE_SEARCH_OTHER_OWNER": json.dumps(other_owner),
        }
        exit_code = await asyncio.to_thread(_run_process, command, cwd=context.project_root,
            log=artifacts / "native-quick-access-driver.log", timeout=195, environment=environment)
    assert exit_code == 0, f"Electron Quick Access 原生验收失败：{artifacts}"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete"
    assert result["quickAccessShortcut"] and result["commandCenterOpensQuickAccess"]
    assert result["scopedContentSearch"] and result["explicitScopeSearch"]
    assert result["searchEditorScopeRestored"]
    assert result["searchRequests"]
    assert all(item["owners"] and item["request_id"] for item in result["searchRequests"])
    assert any(item["owners"] == [owner["workspace_id"]] for item in result["searchRequests"])
    expected_owners = sorted([owner["workspace_id"], other_owner["workspace_id"]])
    assert any(item["owners"] == expected_owners for item in result["searchRequests"])
    assert len(result["native"]["launches"]) == len(result["native"]["exits"]) == 2
    assert len({entry["url"] for entry in result["native"]["launches"]}) == 1
    assert len({entry["location"]["userData"] for entry in result["native"]["launches"]}) == 1
    assert all(entry["code"] == 0 and entry["signal"] is None for entry in result["native"]["exits"])
    assert result["pageErrors"] == []
