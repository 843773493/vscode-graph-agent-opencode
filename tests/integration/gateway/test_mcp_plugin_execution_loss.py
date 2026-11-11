"""真实 Backend 中断后，插件存活与原执行丢失分别结算。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import cast

import httpx
import psutil
import pytest

from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.integration.backend.agents.test_mcp_tool_approval import (
    read_approvals as read_approvals,  # noqa: PLC0414 - 原产品确认入口
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    McpPluginCase,
    _catalog,
    _plugin,
    _publish,
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_case as mcp_plugin_case,  # noqa: PLC0414 - 独立真实 Gateway／Backend
)
from tests.integration.gateway.test_service_plugin_lifecycle import _same_process
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.plugin_package import install_plugin_async
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


@pytest.fixture
def mcp_plugin_tool_restrictions(boundary: str) -> dict[str, list[str]]:
    # 确认策略属于启动配置；关键执行／审批状态仍全部由产品消息入口建立。
    return {"confirmation_required": ["mcp__probe__probe"]} if boundary == "approval" else {}


@pytest.mark.parametrize("boundary", ["approval", "inflight"])
async def test_backend_loss_keeps_plugin_and_never_replays_original_call(
    mcp_plugin_case: McpPluginCase, prepared_mcp_stdio_package: Path, read_approvals, boundary: str,
) -> None:
    case = mcp_plugin_case
    artifacts = case.context.artifacts_dir
    definitions, receipts = artifacts / "definitions.json", artifacts / "receipts.jsonl"
    definitions.write_text('{"probe":{"type":"object"},"second":{"type":"object"}}', encoding="utf-8")
    await install_plugin_async(lambda path, payload: case.client.post(path, json=payload),
        gateway_id=case.gateway_id, path=str(prepared_mcp_stdio_package))
    await _publish(case, {"plugins": {
        "runtime": {"entries": [{"package_id": "boxteam.mcp-stdio", "package_version": "0.1.0",
            "plugin_id": "stdio", "enabled": True}]},
        "configuration": {"values": {"boxteam.mcp-stdio": {"stdio": {
            "command": sys.executable,
            "args": [str(Path.cwd() / "tests/integration/workspace_services/mcp/schema_server.py"),
                str(definitions), str(receipts)], "cwd": str(Path.cwd()),
        }}}},
    }}, f"loss-enable-{boundary}")
    original_plugin = await _plugin(case, state="ready")
    await _publish(case, {"mcp": {"servers": {"probe": {
        "enabled": True, "transport": "plugin", "service": {
            "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
            "package_id": "boxteam.mcp-stdio", "package_version": "0.1.0",
            "plugin_id": "stdio", "service_id": "mcp",
        },
    }}}}, f"loss-connect-{boundary}")
    before_catalog = await _catalog(case)
    session = await create_session(case.client, f"插件执行中断 {boundary}")
    thread = (await case.client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]

    def plan(phase: str, *, complete: bool) -> tuple[list[str], str]:
        call_id = f"loss-{boundary}-{phase}"
        calls = [{"id": f"{call_id}-discover", "type": "function", "function": {
            "name": "search_tools", "arguments": json.dumps({"tool_ids": ["mcp__probe__probe"]}),
        }}, {"id": call_id, "type": "function", "function": {
            "name": "invoke_extension_tool", "arguments": json.dumps({"tool_name": "mcp__probe__probe",
                "arguments": {"proof": phase, **({"call_mode": "wait"} if phase == "lost" and boundary == "inflight" else {})}}),
        }}]
        deltas: list[dict[str, object]] = [{"tool_calls": [call]} for call in calls]
        if complete:
            deltas.append({"content": "独立新执行已完成。"})
        interactions = [cast(dict[str, list[dict[str, object]]], build_cassette(
            asset_id=f"{call_id}-{index}", model="mcp-plugin-model",
            url="https://opencode.ai/zen/v1/chat/completions",
            frames=chat_completion_frames(response_id=f"chatcmpl-{call_id}-{index}", model="mcp-plugin-model",
                deltas=({"role": "assistant", **delta},),
                finish_reason="tool_calls" if "tool_calls" in delta else "stop"),
        ).raw)["interactions"][0] for index, delta in enumerate(deltas)]
        return case.replay.control.plan_model_calls(session, thread, interactions), call_id

    async def send(phase: str) -> str:
        response = await case.client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": f"发现并调用 probe，验证 {phase}。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        }, headers={"X-Request-ID": f"loss-{boundary}-{phase}"})
        response.raise_for_status()
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == f"loss-{boundary}-{phase}"
        return response.json()["data"]["job_id"]

    lost_calls, lost_call = plan("lost", complete=False)
    lost_job = await send("lost")
    approval = None
    if boundary == "approval":
        approval, = await read_approvals(case.client, lost_job)
        assert [action["tool_call_id"] for action in approval["actions"]] == [lost_call]
        assert not receipts.exists()
    else:
        async with asyncio.timeout(30):
            while not receipts.exists():
                await asyncio.sleep(0.05)
        assert json.loads(receipts.read_text().splitlines()[0])["arguments"] == {"proof": "lost", "call_mode": "wait"}
        assert (await case.client.get(f"/api/v1/jobs/{lost_job}/approvals")).json()["data"] == []

    targets = (await case.client.get("/api/gateway/workspaces")).json()["data"]["items"]
    target = next(item for item in targets if item["workspace_id"] == case.workspace_id)
    # 只用已有本机诊断取得本测试 Backend 的实际 PID；业务始终经过 Gateway。
    async with httpx.AsyncClient(headers=LOCAL_TOKEN_HEADERS, trust_env=False, timeout=5) as diagnostic:
        runtime = (await diagnostic.get(target["services"]["workspace_api"]["local_url"].rstrip("/")
            + "/api/v1/internal/runtime-logging")).raise_for_status().json()["data"]
    backend = psutil.Process(runtime["pid"])
    assert backend.pid != original_plugin["instance"]["process_id"]
    backend.kill()
    async with asyncio.timeout(10):
        while _same_process(backend):
            await asyncio.sleep(0.05)
    assert (await _plugin(case, state="ready"))["instance"] == original_plugin["instance"]
    restarted = await case.client.post(f"/api/gateway/workspaces/{case.workspace_id}/runtime/start", timeout=120)
    restarted.raise_for_status()
    assert (await _plugin(case, state="ready"))["instance"] == original_plugin["instance"]
    after_catalog = await _catalog(case)
    assert after_catalog["connection_ref"] != before_catalog["connection_ref"]
    assert after_catalog["tools"] == before_catalog["tools"]
    traces = (await case.client.get(f"/api/v1/sessions/{session}/traces", params={"thread_id": thread})).json()["data"]["items"]
    loss, = [event for event in traces if event["job_id"] == lost_job and event["type"] == "session_interrupted"]
    assert loss["raw"]["payload"]["code"] == "execution_lost"
    assert (await case.client.get("/api/v1/jobs/tool-approvals", params={"session_id": session, "thread_id": thread})).json()["data"] == []
    if approval is not None:
        late = await case.client.post(f"/api/v1/jobs/{lost_job}/approvals", json={
            "command_id": "lost-plugin-handle", "checkpoint_id": approval["checkpoint_id"], "responses": [{
                "approval_id": approval["approval_id"], "decisions": [{"tool_call_id": lost_call, "type": "approve"}],
            }],
        })
        assert late.status_code == 404 and lost_job in late.text, late.text
        assert not receipts.exists()

    new_calls, new_call = plan("new", complete=True)
    new_job = await send("new")
    assert new_job != lost_job
    if approval is not None:
        current, = await read_approvals(case.client, new_job)
        assert current["approval_id"] != approval["approval_id"]
        response = await case.client.post(f"/api/v1/jobs/{new_job}/approvals", json={
            "command_id": "new-plugin-execution", "checkpoint_id": current["checkpoint_id"], "responses": [{
                "approval_id": current["approval_id"], "decisions": [{"tool_call_id": new_call, "type": "approve"}],
            }],
        })
        response.raise_for_status()
    await wait_for_job_done(case.client, new_job, max_attempts=60)
    plugin_crash: dict[str, object] | None = None
    resumed_calls: list[str] = []
    resumed_job: str | None = None
    if boundary == "inflight":
        process = psutil.Process(original_plugin["instance"]["process_id"])
        children = process.children(recursive=True)
        assert children, "真实 stdio Server 必须由插件持有"
        process.kill()
        failed = await _plugin(case, state="failed")
        assert failed["binding"]["enabled"] is True
        assert failed["instance"] == original_plugin["instance"]
        old_route = (f"/api/gateway/service-plugins/boxteam.mcp-stdio/stdio/instances/"
            f"{original_plugin['instance']['instance_id']}/services/mcp/?package_version=0.1.0"
            f"&workspace_id={case.workspace_id}")
        rejected = await case.client.post(old_route, json={"jsonrpc": "2.0", "id": "old-instance", "method": "tools/list"})
        assert rejected.status_code == 409, rejected.text
        assert (await _plugin(case, state="failed"))["instance"] == original_plugin["instance"], "状态或业务访问不能重启插件"
        restarted_plugin = await case.client.post("/api/gateway/service-plugins/restart", json={
            "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
            "package_id": "boxteam.mcp-stdio", "package_version": "0.1.0", "plugin_id": "stdio",
            "revision": failed["revision"],
        })
        restarted_plugin.raise_for_status()
        replacement = restarted_plugin.json()["data"]
        assert replacement["state"] == "ready" and replacement["instance"]["instance_id"] != original_plugin["instance"]["instance_id"]
        async with asyncio.timeout(10):
            while any(_same_process(owned) for owned in [process, *children]):
                await asyncio.sleep(0.05)
        assert (await case.client.post(old_route, json={"jsonrpc": "2.0", "id": "old-instance", "method": "tools/list"})).status_code == 409
        reconnect = await case.client.post("/api/v1/mcp/connection-operations", json={
            "operation_id": "loss-explicit-plugin-reconnect", "server_id": "probe", "action": "connect",
        })
        reconnect.raise_for_status()
        async with asyncio.timeout(15):
            while True:
                operation_response = await case.client.get("/api/v1/mcp/connection-operations/loss-explicit-plugin-reconnect",
                    params={"wait_timeout_ms": 1000})
                operation_response.raise_for_status()
                operation = operation_response.json()["data"]
                if operation["status"] == "succeeded":
                    break
                assert operation["status"] in {"pending", "verifying"}, operation
        replacement_catalog = await _catalog(case)
        assert replacement_catalog["connection_ref"] != after_catalog["connection_ref"]
        assert replacement_catalog["tools"] == after_catalog["tools"]
        resumed_calls, _ = plan("after-plugin-crash", complete=True)
        resumed_job = await send("after-plugin-crash")
        await wait_for_job_done(case.client, resumed_job, max_attempts=60)
        plugin_crash = {"failed": failed, "replacement": replacement, "catalog": replacement_catalog,
            "old_route_status": rejected.status_code, "old_children": [owned.pid for owned in children],
            "new_job": resumed_job}
    actual = [json.loads(line) for line in receipts.read_text().splitlines()]
    assert [row["arguments"] for row in actual if not row.get("cancelled")] == ([
        {"proof": "lost", "call_mode": "wait"},
    ] if boundary == "inflight" else []) + [{"proof": "new"}] + ([{"proof": "after-plugin-crash"}] if resumed_job else [])
    with RolloutCheckpointSaver(case.workspace_root / ".boxteam/sessions") as cold:
        with cold._storage.storage.read_connection(session, thread) as connection:
            executions = connection.execute("SELECT job_id,outcome FROM executions ORDER BY created_at").fetchall()
        assert executions == [(lost_job, "execution_lost"), (new_job, "completed")] + ([(resumed_job, "completed")] if resumed_job else [])
        unknown, = [item for item in cold._storage.storage.read_items(session, thread)
            if item.semantic_kind == "tool_result" and item.tool_call_id is not None
            and item.projection_identity is not None
            and provider_tool_call_id(item.projection_identity, item.tool_call_id) == lost_call]
        assert unknown.projection_identity is not None and unknown.tool_call_id is not None
        assert provider_tool_call_id(unknown.projection_identity, unknown.tool_call_id) == lost_call
        assert unknown.projection_identity.model_call_id == lost_calls[-1]
        assert isinstance(unknown.payload, dict) and unknown.payload["tool_outcome"] == "unknown"
        assert unknown.payload["execution_confirmed"] is False
        for call_id in [*lost_calls, *new_calls, *resumed_calls]:
            record = json.loads((case.replay.artifacts / f"{call_id}.json").read_text())
            with cold._storage.storage.read_connection(session, thread) as connection:
                assembly_id, = connection.execute("SELECT assembly_id FROM context_assemblies WHERE model_call_id=?",
                    (record["identity"]["model_call_id"],)).fetchone()
            assembly = cold.get_context_assembly(session, thread, assembly_id=assembly_id)
            assert assembly.request_hash_preimage == normalize_wire_request(record["request"])
            assembly.validate_hashes()
    await _publish(case, {"mcp": {"servers": {}}, "plugins": {"runtime": {"entries": []}}}, "loss-unbind")
    response = await case.client.post("/api/gateway/service-plugins/uninstall", json={
        "gateway_id": case.gateway_id, "package_id": "boxteam.mcp-stdio", "package_version": "0.1.0",
    })
    response.raise_for_status()
    assert receipts.is_file(), "物理卸载不能删除业务收据"
    (artifacts / "execution-loss.json").write_text(json.dumps({"boundary": boundary,
        "plugin": original_plugin, "catalog_before": before_catalog, "catalog_after": after_catalog,
        "backend_pid": backend.pid, "lost_job": lost_job, "new_job": new_job,
        "loss": loss, "receipts": actual, "plugin_crash": plugin_crash, "uninstall": response.json(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
