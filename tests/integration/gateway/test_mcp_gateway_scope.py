"""两个工作区经真实 Gateway 共用插件进程，各自保有 MCP 会话和历史。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import commentjson
import httpx

from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    McpPluginCase,
    _agent_call,
    _assert_tool_skill_source,
    _publish,
    _skill_package,
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_case as mcp_plugin_case,  # noqa: PLC0414 - 复用真实产品装具
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_tool_restrictions as mcp_plugin_tool_restrictions,  # noqa: PLC0414 - mcp_plugin_case 的 fixture 依赖
)
from tests.support.plugin_package import install_plugin_async
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


def gateway_mcp_package(path: Path, prepared_package: Path) -> Path:
    prepared = _skill_package(path.with_suffix(".skills.zip"), prepared_package)
    with zipfile.ZipFile(prepared) as source, zipfile.ZipFile(path, "w") as archive:
        manifest = commentjson.loads(source.read("plugin.jsonc").decode())
        plugin, = manifest["plugins"]
        plugin.update(instance_scope="gateway", entrypoint=["shared_http.py"])
        plugin["contributes"]["configuration"] = {"type": "object", "additionalProperties": False,
            "required": ["definitions", "receipts", "calls"],
            "properties": {key: {"type": "string"} for key in ("definitions", "receipts", "calls")}}
        for entry in source.infolist():
            if entry.filename == "bridge.py":
                continue
            archive.writestr(entry, json.dumps(manifest).encode() if entry.filename == "plugin.jsonc" else source.read(entry))
        archive.writestr("shared_http.py", Path("tests/support/mcp/gateway_plugin.py").read_bytes())
    prepared.unlink()
    return path


async def bind_gateway_mcp(case: McpPluginCase, *, enabled: bool = True) -> dict:
    path = "/api/gateway/config/sources/gateway_mutable_override"
    document = (await case.client.get(path)).raise_for_status().json()["data"]
    source = commentjson.loads(document["jsonc"])
    artifacts = case.context.artifacts_dir
    source.setdefault("plugins", {}).update({
        "runtime": {"entries": [{"package_id": "boxteam.mcp-stdio", "package_version": "0.1.0",
            "plugin_id": "stdio", "enabled": enabled}]},
        "configuration": {"values": {"boxteam.mcp-stdio": {"stdio": {
            key: str(artifacts / f"shared-{key}.jsonl") for key in ("definitions", "receipts", "calls")}}}},
    })
    response = await case.client.put(path, json={"gateway_id": case.gateway_id,
        "source_key": "gateway_mutable_override", "jsonc": json.dumps(source),
        "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"], "idempotency_key": f"shared-mcp-{enabled}"})
    response.raise_for_status()
    assert response.json()["data"]["edit"]["state"] == "active", response.text
    status = (await case.client.post("/api/gateway/service-plugins/status",
        json={"gateway_id": case.gateway_id})).raise_for_status().json()["data"]
    slot, = [item for item in status["plugins"] if item["binding"]["package_id"] == "boxteam.mcp-stdio"]
    assert slot["binding"].get("workspace_id") is None
    assert slot["state"] == ("ready" if enabled else "stopped"), slot
    return slot


async def test_gateway_mcp_shares_process_without_merging_workspace_sessions(
    mcp_plugin_case: McpPluginCase, prepared_mcp_stdio_package: Path,
) -> None:
    case = mcp_plugin_case
    artifacts = case.context.artifacts_dir
    (artifacts / "shared-definitions.jsonl").write_text(
        '{"probe":{"type":"object"},"second":{"type":"object"}}', encoding="utf-8")
    package = gateway_mcp_package(artifacts / "shared-mcp.zip", prepared_mcp_stdio_package)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    await install_plugin_async(lambda path, body: case.client.post(path, json=body),
        gateway_id=case.gateway_id, path=str(package), expected_digest=digest)
    slot = await bind_gateway_mcp(case)
    second = prepare_default_test_workspace(workspace_root=case.workspace_root / "second-project",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True)
    second_config = install_test_workspace_config(workspace_root=second,
        config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    # 两个工作区使用同一回放提供方，身份与业务存储仍由各自产品入口创建。
    second_config.write_bytes((case.workspace_root / ".boxteam/workspace.jsonc").read_bytes())
    response = await case.client.post("/api/gateway/managed-workspaces", json={
        "root_path": str(second), "name": "Shared MCP second", "create_directory": False})
    response.raise_for_status()
    second_id = next(item["workspace_id"] for item in response.json()["data"]["items"] if item["name"] == "Shared MCP second")
    headers = case.client.headers.copy()
    headers["X-BoxTeam-Workspace-Id"] = second_id
    async with httpx.AsyncClient(base_url=case.client.base_url, headers=headers, cookies=case.client.cookies,
                                 timeout=90, trust_env=False) as client:
        sibling = replace(case, client=client, workspace_id=second_id, workspace_root=second)
        service = {"gateway_id": case.gateway_id, "package_id": "boxteam.mcp-stdio",
            "package_version": "0.1.0", "plugin_id": "stdio", "service_id": "mcp"}
        for index, owner in enumerate((case, sibling)):
            await _publish(owner, {"mcp": {"servers": {"probe": {
                "enabled": True, "transport": "plugin", "service": service}}}}, f"shared-connect-{index}")
        catalogs = []
        for owner in (case, sibling):
            response = await owner.client.get("/api/v1/mcp/servers", params={"refresh": "false"})
            response.raise_for_status()
            server, = response.json()["data"]
            assert server["status"] == "ready", server
            assert server["service"] == {**service, "workspace_id": None}
            assert {tool["remote_name"] for tool in server["tools"]} == {"probe", "second"}
            catalogs.append(server)
        assert catalogs[0]["connection_ref"] != catalogs[1]["connection_ref"]
        source = {**service, "workspace_id": None, "package_digest": digest,
            "instance_id": slot["instance"]["instance_id"], "contract_version": 1}
        records = await asyncio.gather(_agent_call(case, phase="shared-first"), _agent_call(sibling, phase="shared-second"))
        for owner, history, phase in zip((case, sibling), records, ("shared-first", "shared-second"), strict=True):
            await _assert_tool_skill_source(owner, history, phase=phase, source=source)
        calls = [json.loads(line) for line in (artifacts / "shared-calls.jsonl").read_text().splitlines()]
        assert len(calls) == 4, calls
        assert {call["pid"] for call in calls} == {slot["instance"]["process_id"]}
        sessions = {}
        for workspace_id, phase in ((case.workspace_id, "shared-first"), (second_id, "shared-second")):
            own = [call for call in calls if call["workspace_id"] == workspace_id]
            assert len(own) == 2 and {call["arguments"]["phase"] for call in own} == {phase}, own
            session_ids = {call["session_id"] for call in own}
            assert len(session_ids) == 1
            sessions[workspace_id] = session_ids.pop()
        assert len(set(sessions.values())) == 2
        # 关闭一个工作区的逻辑连接，不关闭共享实例或另一个工作区的连接。
        await _publish(case, {"mcp": {"servers": {}}}, "shared-disconnect-first")
        response = await sibling.client.get("/api/v1/mcp/servers", params={"refresh": "false"})
        response.raise_for_status()
        assert response.json()["data"][0]["connection_ref"] == catalogs[1]["connection_ref"]
        await _agent_call(sibling, phase="shared-survivor")
        status = (await case.client.post("/api/gateway/service-plugins/status",
            json={"gateway_id": case.gateway_id})).raise_for_status().json()["data"]
        current, = [item for item in status["plugins"] if item["binding"]["package_id"] == "boxteam.mcp-stdio"]
        assert current["instance"] == slot["instance"]
        calls = [json.loads(line) for line in (artifacts / "shared-calls.jsonl").read_text().splitlines()]
        assert len(calls) == 6
        assert all(call["session_id"] == sessions[second_id] and call["workspace_id"] == second_id for call in calls[-2:])
        stopped = await bind_gateway_mcp(case, enabled=False)
        assert stopped["instance"] is None
        async with asyncio.timeout(10):
            while True:
                response = await sibling.client.get("/api/v1/mcp/servers", params={"refresh": "false"})
                if response.status_code == 503:
                    # 原目录 API 对 session 故障返回明确失败，不能继续展示 ready。
                    assert "probe" in response.json()["detail"]
                    disconnected = response.json()
                    break
                response.raise_for_status()
                disconnected, = response.json()["data"]
                if disconnected["status"] != "ready":
                    break
                await asyncio.sleep(0.05)
        if response.is_success:
            assert disconnected["connection_ref"] is None
        (artifacts / "shared-scope.json").write_text(json.dumps({"slot": slot,
            "catalogs": catalogs, "sessions": sessions, "calls": calls,
            "stopped": stopped, "disconnected": disconnected}, indent=2), encoding="utf-8")
