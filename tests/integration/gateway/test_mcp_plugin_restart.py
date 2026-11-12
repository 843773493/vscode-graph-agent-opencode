"""显式重启插件后旧实例失效，只有显式重连建立新的 Backend generation。"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import psutil

from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    McpPluginCase,
    _catalog,
    _package,
    _plugin,
    _publish,
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_case as mcp_plugin_case,  # noqa: PLC0414 - 共享真实 Gateway 产品装具
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_tool_restrictions as mcp_plugin_tool_restrictions,  # noqa: PLC0414 - mcp_plugin_case 的 fixture 依赖
)
from tests.support.plugin_package import install_plugin_async


async def test_explicit_plugin_restart_invalidates_old_instance_without_automatic_replay(
    mcp_plugin_case: McpPluginCase, prepared_mcp_stdio_package: Path,
):
    case = mcp_plugin_case
    artifacts = case.context.artifacts_dir
    archive = _package(artifacts / "mcp-stdio.zip", prepared_mcp_stdio_package)
    await install_plugin_async(
        lambda path, payload: case.client.post(path, json=payload),
        gateway_id=case.gateway_id,
        path=str(archive),
        expected_digest=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    definitions, receipts = artifacts / "definitions.json", artifacts / "receipts.jsonl"
    definitions.write_text('{"probe":{"type":"object"},"second":{"type":"object"}}', encoding="utf-8")
    await _publish(case, {"plugins": {
        "runtime": {"entries": [{"package_id": "boxteam.mcp-stdio", "package_version": "0.1.0", "plugin_id": "stdio", "enabled": True}]},
        "configuration": {"values": {"boxteam.mcp-stdio": {"stdio": {
            "command": sys.executable,
            "args": [str(Path.cwd() / "tests/integration/workspace_services/mcp/schema_server.py"), str(definitions), str(receipts)],
            "cwd": str(Path.cwd()),
        }}}},
    }}, "restart-plugin-enable")
    previous = await _plugin(case, state="ready")
    await _publish(case, {"mcp": {"servers": {"probe": {
        "enabled": True, "transport": "plugin", "service": {
            "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
            "package_id": "boxteam.mcp-stdio", "package_version": "0.1.0", "plugin_id": "stdio", "service_id": "mcp",
        },
    }}}}, "restart-plugin-connect")
    old_catalog = await _catalog(case)
    old_instance = previous["instance"]
    process = psutil.Process(old_instance["process_id"])
    processes = [process, *process.children(recursive=True)]
    response = await case.client.post("/api/gateway/service-plugins/restart", json={
        "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
        "package_id": "boxteam.mcp-stdio", "package_version": "0.1.0", "plugin_id": "stdio",
        "revision": previous["revision"],
    })
    response.raise_for_status()
    current = response.json()["data"]
    assert current["state"] == "ready", current
    assert current["instance"]["instance_id"] != old_instance["instance_id"]
    _, alive = await asyncio.to_thread(psutil.wait_procs, processes, timeout=5)
    assert not alive, [item.pid for item in alive]
    old_route = (f"/api/gateway/service-plugins/boxteam.mcp-stdio/stdio/instances/{old_instance['instance_id']}/services/mcp/"
                 f"?package_version=0.1.0&workspace_id={case.workspace_id}")
    response = await case.client.post(old_route, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 409, response.text
    # 旧 session 已先退出时返回失败快照；刷新撞上断线时返回原刷新错误，均不能改投新实例。
    response = await case.client.get("/api/v1/mcp/servers", params={"refresh": "true"})
    if response.status_code == 200:
        failed, = [item for item in response.json()["data"] if item["server_id"] == "probe"]
        assert failed["status"] == "failed" and failed["connection_ref"] is None, failed
        assert failed["tools"] == old_catalog["tools"], "失败状态保留原目录，不能自动改投新服务"
    else:
        assert response.status_code == 503 and "probe" in response.json()["detail"], response.text
    response = await case.client.post("/api/v1/mcp/connection-operations", json={
        "operation_id": "restart-explicit-connect", "server_id": "probe", "action": "connect",
    })
    response.raise_for_status()
    deadline = asyncio.get_running_loop().time() + 15
    while True:
        response = await case.client.get("/api/v1/mcp/connection-operations/restart-explicit-connect",
                                         params={"wait_timeout_ms": 1000})
        response.raise_for_status()
        operation = response.json()["data"]
        if operation["status"] == "succeeded":
            break
        assert operation["status"] in {"pending", "verifying"}, operation
        assert asyncio.get_running_loop().time() < deadline, operation
    new_catalog = await _catalog(case)
    assert new_catalog["connection_ref"] != old_catalog["connection_ref"]
    assert not receipts.exists(), "重启、失效读取和重连不能自动重放工具副作用"

    restarted_instance = psutil.Process(current["instance"]["process_id"])
    restarted_children = restarted_instance.children(recursive=True)
    case.restart_gateway()
    _, alive = await asyncio.to_thread(
        psutil.wait_procs, [restarted_instance, *restarted_children], timeout=5,
    )
    assert not alive, [process.pid for process in alive]
    cold_plugin = await _plugin(case, state="ready")
    assert cold_plugin["instance"]["instance_id"] != current["instance"]["instance_id"]
    await _catalog(case)
    assert not receipts.exists(), "同 Home 冷启动不能重放 MCP 工具副作用"
