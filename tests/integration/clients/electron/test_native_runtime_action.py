"""真实 Electron Quick Access 经 Gateway 停止插件托管终端并核对权威回读。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import commentjson
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
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import TestFilePorts
from tests.support.terminal_plugin import (
    copy_prepared_developer_tools_package,
    enable_developer_tools_plugins,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(scope="module", autouse=True)
def native_runtime_action_service_ports(electron_service_ports: TestFilePorts) -> None:
    """在 Gateway fixture 和 Electron 代理使用端口前持有原租约。"""


async def test_packaged_electron_quick_access_stops_runtime_resource(
    request: pytest.FixtureRequest,
    packaged_electron: Path,
    electron_service_ports: TestFilePorts,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, object],
    prepared_developer_tools_package: Path,
) -> None:
    client, context, _workspace, _provider = metadata_case
    assert sys.platform == "linux"
    copy_prepared_developer_tools_package(prepared_developer_tools_package,
        context.boxteam_home_for_node(request.node.nodeid))
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root / context.run_root.name / "native-runtime-action",
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        fresh_identity=True,
    )
    config_path = install_test_workspace_config(workspace_root=workspace,
        config_path=context.project_root / "configs/tests/workspace/default.jsonc",
        schema_path=context.project_root / "configs/workspace_schema.jsonc")
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    config["settings"]["workspace"].setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})[
        "url"
    ] = str(client.base_url).rstrip("/")
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    registered = (await client.post("/api/gateway/managed-workspaces", json={
        "root_path": str(workspace), "name": "Native Runtime Action",
    })).raise_for_status().json()["data"]
    owner = next(item for item in registered["items"] if item["name"] == "Native Runtime Action")
    enabled = await enable_developer_tools_plugins(client, gateway_id=owner["gateway_id"],
        workspace_id=owner["workspace_id"], plugin_ids=("terminal", "browser"))
    assert all(plugin["state"] == "ready" for plugin in enabled.values())
    owner_headers = {"X-BoxTeam-Target-Gateway-Id": owner["gateway_id"],
        "X-BoxTeam-Workspace-Id": owner["workspace_id"]}
    async with httpx.AsyncClient(base_url=str(client.base_url), headers=client.headers, timeout=30) as api:
        created_response = await api.post("/api/v1/sessions", headers=owner_headers,
            json={"title": "原生动作来源", "agent_id": "default"})
        assert created_response.status_code == 200, created_response.text
        created = created_response.json()["data"]
        main_response = await api.get(f"/api/v1/sessions/{created['session_id']}/main-thread", headers=owner_headers)
        assert main_response.status_code == 200, main_response.text
        main = main_response.json()["data"]
        resource_response = await api.post("/api/v1/resources/terminals", headers=owner_headers, json={
            "session_id": created["session_id"], "thread_id": main["thread_id"],
            "title": "原生 Quick Access 终端", "retention_scope": "workspace",
        })
        assert resource_response.status_code == 200, resource_response.text
        resource = resource_response.json()["data"]
        assert resource["status"] == "running"
    artifacts = context.artifacts_dir
    runtime = context.runtime_root_for_node(request.node.nodeid)
    result_path = artifacts / "native-runtime-action.json"
    node = shutil.which("node")
    assert node is not None, "Playwright Electron inspector 需要 Node"
    command = [sys.executable, str(context.project_root / "app/runtime/posix_job.py"),
        "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--", "xvfb-run", "-a",
        node, "tests/integration/clients/electron/native_runtime_action.mjs"]
    with chromium_temporary_environment(runtime / "native-tmp") as temporary:
        environment = {**os.environ, **temporary,
            "BOXTEAM_ELECTRON_EXECUTABLE": str(packaged_electron),
            "BOXTEAM_ELECTRON_GATEWAY": str(client.base_url).rstrip("/"),
            "BOXTEAM_ELECTRON_PROXY_PORT": str(electron_service_ports.port(21)),
            "BOXTEAM_ELECTRON_RUNTIME": str(runtime),
            "BOXTEAM_ELECTRON_ARTIFACTS": str(artifacts),
            "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
            "BOXTEAM_NATIVE_ACTION_OWNER": json.dumps(owner),
            "BOXTEAM_NATIVE_ACTION_RESOURCE": json.dumps(resource),
        }
        exit_code = await asyncio.to_thread(_run_process, command, cwd=context.project_root,
            log=artifacts / "native-runtime-action-driver.log", timeout=195, environment=environment)
    assert exit_code == 0, f"Electron Quick Access 运行资源动作验收失败：{artifacts}"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete" and result["quickAccessRuntimeAction"]
    assert result["pageErrors"] == []
    assert result["stop"]["owner"] == {"gateway_id": owner["gateway_id"], "workspace_id": owner["workspace_id"]}
    assert result["stop"]["request_id"] == result["stop"]["header"]
    assert result["stop"]["result"]["status"] == result["stop"]["readback"]["status"] == "terminated"
    assert len(result["native"]["launches"]) == len(result["native"]["exits"]) == 1
    assert all(item["code"] == 0 and item["signal"] is None for item in result["native"]["exits"])
