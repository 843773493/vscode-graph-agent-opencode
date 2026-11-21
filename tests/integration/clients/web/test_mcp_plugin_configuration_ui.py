"""正式 Workbench 的 MCP 配置、资源发现及连接动作共用原 owner。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
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
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.mcp.http_endpoint import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 官方 SDK HTTP 服务
)
from tests.support.paths import chromium_temporary_environment
from tests.support.plugin_package import install_plugin_async
from tests.support.ports import integration_port_block_for_file


@pytest.fixture(autouse=True)
def mcp_ui_ports(request: pytest.FixtureRequest) -> Iterator[None]:
    # 高端口块也属于客户端临时端口范围；启动前持续保留绑定，避免自己发出的连接抢占。
    context = TestRunContext.from_test_file(Path(request.node.path))
    ports = integration_port_block_for_file(Path(request.node.path))
    lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "ports.json",
                            (ports.port(20), ports.port(21)))
    lease.acquire()
    try:
        yield
    finally:
        lease.release()


@pytest.fixture(autouse=True)
def mcp_ui_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_MCP_UI_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "out/main.js").is_file() and (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (context.artifacts_dir / "assets.json").write_text(json.dumps({
        "path": str(assets), "prepared": bool(prepared),
        "main_sha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest(),
        "version": json.loads((assets / "version.json").read_text(encoding="utf-8")),
    }, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("scope", ["workspace", "gateway"])
@pytest.mark.parametrize("scenario", ["full", "service-management"])
async def test_add_plugin_service_and_http_from_original_workbench(
    mcp_plugin_case: McpPluginCase, http_schema_endpoint_factory, prepared_mcp_stdio_package: Path, scope: str, scenario: str,
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
            "runtime": {"entries": [{"package_id": "boxteam.mcp-stdio", "package_version": "0.1.1", "plugin_id": "stdio", "enabled": True}]},
            "configuration": {"values": {"boxteam.mcp-stdio": {"stdio": {
                "command": sys.executable,
                "args": [str(Path.cwd() / "tests/integration/workspace_services/mcp/schema_server.py"), str(definitions), str(receipts)],
                "cwd": str(Path.cwd()),
            }}}},
        }}, "ui-enable-plugin")
        await _plugin(case, state="ready")
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "MCP Add 页面验收需要 Chromium"
    node = shutil.which("node")
    assert node, "MCP Workbench Playwright 驱动需要发行 Node"
    async with http_schema_endpoint_factory("ui-external") as external:
        browser_artifacts = artifacts / "browser"
        browser_artifacts.mkdir()
        command = [node, "tests/integration/clients/web/mcp_plugin_configuration_ui.mjs"]
        with chromium_temporary_environment(case.context.runtime_root_for_node("mcp-add-browser") / "chromium-tmp") as temporary:
            result = await asyncio.to_thread(subprocess.run, command, timeout=180,
                cwd=case.context.project_root, capture_output=True, text=True, check=False,
                env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                     "BOXTEAM_BROWSER_ARTIFACTS": str(browser_artifacts), "BOXTEAM_BROWSER_FIXTURE": json.dumps({
                         "baseUrl": str(case.client.base_url), "gatewayId": case.gateway_id,
                         "workspaceId": case.workspace_id, "endpoint": external.url, "scope": scope,
                         "managementOnly": scenario == "service-management",
                     })})
        (browser_artifacts / "browser.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, (browser_artifacts / "browser.log").read_text()
        observed = json.loads((browser_artifacts / "result.json").read_text(encoding="utf-8"))
        if scenario == "service-management":
            assert observed["phase"] == "completed"
            assert observed["mcpServiceManagement"] == {
                "source": scope, "retainedOnError": True, "stableLayout": True,
                "pluginEntryWhileUnavailable": True, "runtimeSurvivesDisconnect": True,
                "connectionSettingsOwner": {"gatewayId": case.gateway_id, "workspaceId": case.workspace_id},
            }
            assert [item["action"] for item in observed["mcpOperations"]] == ["disconnect", "connect"]
            assert len(observed["writes"]) == 2
            assert not receipts.exists() and not external.receipts.exists(), "连接管理不执行业务工具"
            return
        assert observed["phase"] == "completed" and observed["mcpQuickAccessLifecycle"]
        assert [item["action"] for item in observed["mcpOperations"]] == ["disconnect", "connect"]
        assert len({item["operation_id"] for item in observed["mcpOperations"]}) == 2
        assert observed["mcpManagementEntries"] == {
            "owner": {"gatewayId": case.gateway_id, "workspaceId": case.workspace_id},
            "parameterScope": scope, "exactVersion": "0.1.1", "cancelledWithoutWrite": True,
            "resourcesTemplatesUnavailable": True,
        }
        entries = [{"source_id": "management-disabled", "protocol_version": 1, "enabled": False}]
        assert observed["downloadSourceManagement"] == {
            "owner": {"gatewayId": case.gateway_id}, "sourceKey": "gateway_mutable_override",
            "persistedEntries": entries, "originalWriter": True,
            "emptySourceCatalogRequests": 0, "emptySourceExternalRequests": 0,
        }
        assert len(observed["writes"]) == 3
        assert [write.get("workspace_id") for write in observed["writes"]] == [case.workspace_id, case.workspace_id, None]
        assert observed["sourceCatalogRequests"] == []
        response = await case.client.get("/api/gateway/config/sources/gateway_mutable_override")
        response.raise_for_status()
        source = response.json()["data"]
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        assert source["gateway_id"] == case.gateway_id and source["source_key"] == "gateway_mutable_override"
        assert source["source_effective"]["plugins"]["sources"]["entries"] == entries
        for submitted in observed["mcpOperations"]:
            response = await case.client.get(f"/api/v1/mcp/connection-operations/{submitted['operation_id']}")
            response.raise_for_status()
            envelope = response.json()
            assert envelope["request_id"] == response.headers["X-Request-ID"]
            assert envelope["target"]["gateway_id"] == case.gateway_id
            assert envelope["target"]["workspace_id"] == case.workspace_id
            operation = envelope["data"]
            assert (operation["operation_id"], operation["server_id"], operation["action"], operation["status"]) == (
                submitted["operation_id"], "ui-plugin", submitted["action"], "succeeded",
            )
        response = await case.client.get("/api/v1/mcp/servers", params={"refresh": "false"})
        response.raise_for_status()
        servers = {server["server_id"]: server for server in response.json()["data"]}
        assert set(servers) == {"ui-plugin", "ui-http"}, servers
        assert all(server["status"] == "ready" for server in servers.values()), servers
        assert servers["ui-plugin"]["service"]["workspace_id"] == (case.workspace_id if scope == "workspace" else None)
        assert servers["ui-http"]["service"] is None
        assert not receipts.exists() and not external.receipts.exists(), "配置与目录展示不能执行业务工具"
