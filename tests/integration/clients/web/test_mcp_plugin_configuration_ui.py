"""正式 Workbench 的 MCP Add 只保存原工作区的插件服务或外部 HTTP。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式共享构建
)
from tests.integration.gateway.test_mcp_gateway_scope import (
    bind_gateway_mcp,
    gateway_mcp_package,
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    McpPluginCase,
    _package,
    _plugin,
    _publish,
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_case as mcp_plugin_case,  # noqa: PLC0414 - 真实 Gateway 与 Backend
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_tool_restrictions as mcp_plugin_tool_restrictions,  # noqa: PLC0414 - mcp_plugin_case 的 fixture 依赖
)
from tests.support.mcp.http_endpoint import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 官方 SDK HTTP 服务
)
from tests.support.paths import chromium_temporary_environment
from tests.support.plugin_package import install_plugin_async


@pytest.fixture(autouse=True)
def mcp_ui_assets(web_workbench_assets: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))


@pytest.mark.parametrize("scope", ["workspace", "gateway"])
async def test_add_plugin_service_and_http_from_original_workbench(
    mcp_plugin_case: McpPluginCase, http_schema_endpoint_factory, prepared_mcp_stdio_package: Path, scope: str,
) -> None:
    case = mcp_plugin_case
    artifacts = case.context.artifacts_dir
    archive = (_package if scope == "workspace" else gateway_mcp_package)(
        artifacts / "mcp-stdio.zip", prepared_mcp_stdio_package)
    await install_plugin_async(lambda path, body: case.client.post(path, json=body),
        gateway_id=case.gateway_id, path=str(archive),
        expected_digest=hashlib.sha256(archive.read_bytes()).hexdigest())
    definitions = artifacts / ("definitions.json" if scope == "workspace" else "shared-definitions.jsonl")
    receipts = artifacts / ("receipts.jsonl" if scope == "workspace" else "shared-receipts.jsonl")
    definitions.write_text('{"probe":{"type":"object"}}', encoding="utf-8")
    if scope == "gateway":
        await bind_gateway_mcp(case)
    else:
        await _publish(case, {"plugins": {
            "runtime": {"entries": [{"package_id": "boxteam.mcp-stdio", "package_version": "0.1.0", "plugin_id": "stdio", "enabled": True}]},
            "configuration": {"values": {"boxteam.mcp-stdio": {"stdio": {
                "command": sys.executable,
                "args": [str(Path.cwd() / "tests/integration/workspace_services/mcp/schema_server.py"), str(definitions), str(receipts)],
                "cwd": str(Path.cwd()),
            }}}},
        }}, "ui-enable-plugin")
        await _plugin(case, state="ready")
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "MCP Add 页面验收需要 Chromium"
    async with http_schema_endpoint_factory("ui-external") as external:
        browser_artifacts = artifacts / "browser"
        browser_artifacts.mkdir()
        command = ["bun", "tests/integration/clients/web/mcp_plugin_configuration_ui.mjs"]
        with chromium_temporary_environment(case.context.runtime_root_for_node("mcp-add-browser") / "chromium-tmp") as temporary:
            result = await asyncio.to_thread(subprocess.run, command, timeout=120,
                cwd=case.context.project_root, capture_output=True, text=True, check=False,
                env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                     "BOXTEAM_BROWSER_ARTIFACTS": str(browser_artifacts), "BOXTEAM_BROWSER_FIXTURE": json.dumps({
                         "baseUrl": str(case.client.base_url), "gatewayId": case.gateway_id,
                         "workspaceId": case.workspace_id, "endpoint": external.url, "scope": scope,
                     })})
        (browser_artifacts / "browser.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, (browser_artifacts / "browser.log").read_text()
        response = await case.client.get("/api/v1/mcp/servers", params={"refresh": "false"})
        response.raise_for_status()
        servers = {server["server_id"]: server for server in response.json()["data"]}
        assert set(servers) == {"ui-plugin", "ui-http"}, servers
        assert all(server["status"] == "ready" for server in servers.values()), servers
        assert servers["ui-plugin"]["service"]["workspace_id"] == (case.workspace_id if scope == "workspace" else None)
        assert servers["ui-http"]["service"] is None
        assert not receipts.exists() and not external.receipts.exists(), "配置与目录展示不能执行业务工具"
