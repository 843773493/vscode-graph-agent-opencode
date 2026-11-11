"""真实 Gateway 插件参数候选、stdio 替换、目录发布与失败修复。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

import commentjson
import portalocker
import psutil
import pytest

from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    McpPluginCase,
    _agent_call,
    _catalog,
    _package,
    _plugin,
    _publish,
    _publish_expect_failure,
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_case as mcp_plugin_case,  # noqa: PLC0414 - 共用真实 Gateway 产品装具
)
from tests.support.plugin_package import install_plugin_async


async def test_parameter_reconfiguration_orders_plugin_handoff_before_mcp_promotion(
    mcp_plugin_case: McpPluginCase, prepared_mcp_stdio_package: Path,
) -> None:
    case = mcp_plugin_case
    artifacts = case.context.artifacts_dir
    lock = artifacts / "mcp.lock"
    lifecycle = artifacts / "mcp-lifecycle.jsonl"
    definitions = artifacts / "definitions.json"
    receipts = artifacts / "receipts.jsonl"
    database = case.workspace_root / ".boxteam/state/workspace.sqlite"
    server_module = "tests.integration.workspace_services.mcp.single_instance_server"
    definitions.write_text(json.dumps({"probe": {"type": "object"}, "second": {"type": "object"}}),
                           encoding="utf-8")

    archive = _package(artifacts / "mcp-bridge.zip", prepared_mcp_stdio_package)
    await install_plugin_async(
        lambda path, payload: case.client.post(path, json=payload),
        gateway_id=case.gateway_id,
        path=str(archive),
        expected_digest=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )

    await _publish(case, {"plugins": {
        "runtime": {"entries": [{"package_id": "boxteam.mcp-stdio", "package_version": "0.1.0",
                                   "plugin_id": "stdio", "enabled": True}]},
        "configuration": {"values": {"boxteam.mcp-stdio": {"stdio": {
            "command": sys.executable,
            "args": ["-m", server_module, *map(str, (lock, lifecycle, definitions, receipts, database))],
            "cwd": str(Path.cwd()),
            "env": {"MCP_TEST_VERSION": "old"},
        }}}},
    }}, f"mcp-plugin-reconfigure-enable-{uuid4().hex}")
    enabled_plugin = await _plugin(case, state="ready")
    slot_revision = enabled_plugin["revision"]
    restarted = await case.client.post("/api/gateway/service-plugins/restart", json={
        "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
        "package_id": "boxteam.mcp-stdio", "plugin_id": "stdio",
        "package_version": "0.1.0", "revision": slot_revision,
    })
    restarted.raise_for_status()
    assert restarted.json()["data"]["state"] == "ready", restarted.text
    stale = await case.client.post("/api/gateway/service-plugins/restart", json={
        "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
        "package_id": "boxteam.mcp-stdio", "plugin_id": "stdio",
        "package_version": "0.1.0", "revision": enabled_plugin["revision"] + "-stale",
    })
    assert stale.status_code == 409, stale.text
    await _publish(case, {"mcp": {"servers": {"probe": {
        "enabled": True, "transport": "plugin", "service": {
            "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
            "package_id": "boxteam.mcp-stdio", "package_version": "0.1.0",
            "plugin_id": "stdio", "service_id": "mcp",
        },
    }}}}, f"mcp-plugin-reconfigure-connect-{uuid4().hex}")
    before_catalog = await _catalog(case)
    before_plugin = await _plugin(case, state="ready")
    before_pid = before_plugin["instance"]["process_id"]
    with pytest.raises(portalocker.exceptions.LockException), portalocker.Lock(lock, timeout=0):
        pytest.fail("MCP stdio 进程没有持有排他锁")

    await _agent_call(case, phase="before-parameter-change")
    current = await case.client.get("/api/v1/config/sources/workspace_root_mutable_override")
    current.raise_for_status()
    source_document = current.json()["data"]
    source = commentjson.loads(source_document["jsonc"])
    source["plugins"]["configuration"]["values"]["boxteam.mcp-stdio"]["stdio"]["env"]["MCP_TEST_VERSION"] = "new"
    source_response = await case.client.put("/api/v1/config/sources/workspace_root_mutable_override", json={
        "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
        "source_key": "workspace_root_mutable_override", "jsonc": json.dumps(source, ensure_ascii=False),
        "base_file_digest": source_document["file_digest"],
        "base_layer_revision": source_document["layer_revision"],
        "base_layer_digest": source_document["layer_digest"],
        "expected_active_revision": source_document["active_revision"],
        "expected_active_digest": source_document["active_digest"],
        "idempotency_key": f"mcp-plugin-parameter-change-{uuid4().hex}",
    })
    source_response.raise_for_status()
    assert source_response.json()["data"]["edit"]["state"] == "active", source_response.text

    after_plugin = await _plugin(case, state="ready")
    assert after_plugin["instance"]["process_id"] != before_pid
    after_catalog = await _catalog(case)
    assert after_catalog["connection_ref"] != before_catalog["connection_ref"]
    phases = [json.loads(line) for line in lifecycle.read_text(encoding="utf-8").splitlines()]
    starts = [row for row in phases if row["phase"] == "start"]
    assert [row["version"] for row in starts] == ["old", "old", "new"]
    assert len({row["pid"] for row in starts}) == len(starts)
    replaced_server_pid = starts[-2]["pid"]
    stopped_old_pid = next(index for index, row in enumerate(phases)
                           if row["phase"] == "stop" and row["pid"] == replaced_server_pid)
    started_new = next(index for index, row in enumerate(phases)
                       if row["phase"] == "start" and row["version"] == "new")
    assert stopped_old_pid < started_new
    assert any(effect.get("resource") == "workspace-plugin-runtime"
               and effect.get("action") == "candidate-start-intent"
               for effect in phases[-1]["effects"])
    await _agent_call(case, phase="after-parameter-change")

    status_before_failure = await case.client.get("/api/v1/config/reload-status")
    status_before_failure.raise_for_status()
    active_revision = status_before_failure.json()["data"]["active_revision"]
    phase_count = len(phases)
    definitions.write_text(json.dumps({"probe": {"type": "invalid-schema-type"}}), encoding="utf-8")
    failed_key = f"mcp-plugin-bad-directory-{uuid4().hex}"
    failed_publish = await _publish_expect_failure(case, {"plugins": {
        "runtime": {"entries": [{"package_id": "boxteam.mcp-stdio", "package_version": "0.1.0",
                                   "plugin_id": "stdio", "enabled": True}]},
        "configuration": {"values": {"boxteam.mcp-stdio": {"stdio": {
            "command": sys.executable,
            "args": ["-m", server_module, *map(str, (lock, lifecycle, definitions, receipts, database))],
            "cwd": str(Path.cwd()),
            "env": {"MCP_TEST_VERSION": "broken"},
        }}}},
    }}, failed_key)
    assert failed_publish["state"] == "recovery_required"
    source_after_failure = await case.client.get(
        "/api/v1/config/sources/workspace_root_mutable_override", params={"idempotency_key": failed_key},
    )
    source_after_failure.raise_for_status()
    failed_source = source_after_failure.json()["data"]
    assert failed_source["edit"]["state"] == "recovery_required"
    assert failed_source["effective"]["plugins"]["configuration"]["values"]["boxteam.mcp-stdio"]["stdio"]["env"]["MCP_TEST_VERSION"] == "new"
    status = await case.client.get("/api/v1/config/reload-status")
    status.raise_for_status()
    assert status.json()["data"]["state"] == "recovery_required"
    assert not status.json()["data"]["healthy"]
    assert status.json()["data"]["active_revision"] == active_revision
    failed_catalog_response = await case.client.get("/api/v1/mcp/servers", params={"refresh": "false"})
    # 旧实例已退出，active 配置保留不等于旧连接可用；失败目录不能冒充成功发布。
    assert failed_catalog_response.status_code == 503, failed_catalog_response.text
    assert "probe" in failed_catalog_response.text and "McpPluginInstanceChanged" in failed_catalog_response.text
    failed_phases = [json.loads(line) for line in lifecycle.read_text(encoding="utf-8").splitlines()]
    failed_effects = [effect for row in failed_phases[phase_count:] for effect in row["effects"]]
    assert any(effect.get("resource") == "workspace-plugin-runtime"
               and effect.get("action") == "candidate-start-intent" for effect in failed_effects)
    assert not any(effect.get("resource") == "workspace-config-applier"
                   and effect.get("action") == "prepare" for effect in failed_effects)

    definitions.write_text(json.dumps({"probe": {"type": "object"}, "second": {"type": "object"}}),
                           encoding="utf-8")
    source_document = (await case.client.get("/api/v1/config/sources/workspace_root_mutable_override")).json()["data"]
    source = commentjson.loads(source_document["jsonc"])
    source["plugins"]["configuration"]["values"]["boxteam.mcp-stdio"]["stdio"]["env"]["MCP_TEST_VERSION"] = "fixed"
    response = await case.client.put("/api/v1/config/sources/workspace_root_mutable_override", json={
        "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
        "source_key": "workspace_root_mutable_override", "jsonc": json.dumps(source, ensure_ascii=False),
        "base_file_digest": source_document["file_digest"],
        "base_layer_revision": source_document["layer_revision"],
        "base_layer_digest": source_document["layer_digest"],
        "expected_active_revision": source_document["active_revision"],
        "expected_active_digest": source_document["active_digest"],
        "idempotency_key": f"mcp-plugin-repair-directory-{uuid4().hex}",
    })
    response.raise_for_status()
    assert response.json()["data"]["edit"]["state"] == "active", response.text
    repaired_catalog = await _catalog(case)
    assert repaired_catalog["connection_ref"] not in {before_catalog["connection_ref"], after_catalog["connection_ref"]}
    await _agent_call(case, phase="after-repair")


@pytest.mark.parametrize("unbind", [False, True])
async def test_close_cancels_mcp_candidate_without_waiting_for_catalog(
    mcp_plugin_case: McpPluginCase, prepared_mcp_stdio_package: Path, unbind: bool,
) -> None:
    case = mcp_plugin_case
    artifacts = case.context.artifacts_dir
    lock, lifecycle, definitions, receipts = (artifacts / name for name in
        ("mcp.lock", "mcp-lifecycle.jsonl", "definitions.json", "receipts.jsonl"))
    database = case.workspace_root / ".boxteam/state/workspace.sqlite"
    definitions.write_text(json.dumps({"probe": {"type": "object"}, "second": {"type": "object"}}))
    archive = _package(artifacts / "mcp-bridge.zip", prepared_mcp_stdio_package)
    await install_plugin_async(
        lambda path, payload: case.client.post(path, json=payload), gateway_id=case.gateway_id,
        path=str(archive), expected_digest=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    await _publish(case, {"plugins": {
        "runtime": {"entries": [{"package_id": "boxteam.mcp-stdio", "package_version": "0.1.0",
            "plugin_id": "stdio", "enabled": True}]},
        "configuration": {"values": {"boxteam.mcp-stdio": {"stdio": {
            "command": sys.executable,
            "args": ["-m", "tests.integration.workspace_services.mcp.single_instance_server",
                     *map(str, (lock, lifecycle, definitions, receipts, database))],
            "cwd": str(Path.cwd()), "env": {"MCP_TEST_VERSION": "old"},
        }}}},
    }}, f"mcp-close-enable-{uuid4().hex}")
    await _plugin(case, state="ready")
    await _publish(case, {"mcp": {"servers": {"probe": {
        "enabled": True, "transport": "plugin", "service": {
            "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
            "package_id": "boxteam.mcp-stdio", "package_version": "0.1.0",
            "plugin_id": "stdio", "service_id": "mcp",
        },
    }}}}, f"mcp-close-connect-{uuid4().hex}")
    await _catalog(case)

    # 真实目录屏障只阻塞 MCP 候选；关闭必须由原配置 owner 撤销它，不能等 tools/list。
    source_path = "/api/v1/config/sources/workspace_root_mutable_override"
    gate = artifacts / "release-candidate-catalog"
    blocked_key = f"mcp-plugin-blocked-catalog-{uuid4().hex}"
    source_document = (await case.client.get(source_path)).raise_for_status().json()["data"]
    source = commentjson.loads(source_document["jsonc"])
    parameters = source["plugins"]["configuration"]["values"]["boxteam.mcp-stdio"]["stdio"]
    parameters["env"] = {"MCP_TEST_VERSION": "blocked", "MCP_TEST_CATALOG_GATE": str(gate)}
    blocked = asyncio.create_task(case.client.put(source_path, json={
        "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
        "source_key": "workspace_root_mutable_override", "jsonc": json.dumps(source),
        "base_file_digest": source_document["file_digest"],
        "base_layer_revision": source_document["layer_revision"],
        "base_layer_digest": source_document["layer_digest"],
        "expected_active_revision": source_document["active_revision"],
        "expected_active_digest": source_document["active_digest"], "idempotency_key": blocked_key,
    }))
    try:
        async with asyncio.timeout(30):
            while not any(json.loads(line)["phase"] == "catalog-blocked"
                          for line in lifecycle.read_text(encoding="utf-8").splitlines()):
                assert not blocked.done(), (await blocked).text if blocked.done() else ""
                await asyncio.sleep(0.02)
        assert not blocked.done() and not gate.exists()
        before_close = (await case.client.get(source_path)).raise_for_status().json()["data"]
        assert before_close["active_digest"] == source_document["active_digest"]
        close_source = commentjson.loads(before_close["jsonc"])
        if unbind:
            close_source["plugins"]["runtime"]["entries"] = []
        else:
            close_source["plugins"]["runtime"]["entries"][0]["enabled"] = False
        close_request = {
            "gateway_id": case.gateway_id, "workspace_id": case.workspace_id,
            "source_key": "workspace_root_mutable_override", "jsonc": json.dumps(close_source),
            "base_file_digest": before_close["file_digest"],
            "base_layer_revision": before_close["layer_revision"],
            "base_layer_digest": before_close["layer_digest"],
            "expected_active_revision": before_close["active_revision"],
            "expected_active_digest": before_close["active_digest"],
            "idempotency_key": f"mcp-plugin-close-candidate-{uuid4().hex}",
        }
        stale = await case.client.put(source_path, json={**close_request,
            "expected_active_digest": "stale-digest", "idempotency_key": f"stale-close-{uuid4().hex}"})
        assert stale.status_code == 409 and not blocked.done(), stale.text
        blocked_plugin = await _plugin(case, state="ready")
        blocked_pid = next(json.loads(line)["pid"] for line in lifecycle.read_text(encoding="utf-8").splitlines()
                           if json.loads(line)["phase"] == "catalog-blocked")
        async with asyncio.timeout(20):
            closed = await case.client.put(source_path, json=close_request)
            closed.raise_for_status()
            assert closed.json()["data"]["edit"]["state"] == "active", closed.text
            rejected = await blocked
            assert rejected.status_code == 409, rejected.text
            if unbind:
                # refresh 只确认通知受理；实际交接结束后，解绑会删除槽位。
                while True:
                    status = (await case.client.post("/api/gateway/service-plugins/status", json={
                        "gateway_id": case.gateway_id,
                    })).raise_for_status().json()["data"]
                    if not any(item["binding"]["package_id"] == "boxteam.mcp-stdio"
                               and item["binding"].get("workspace_id") == case.workspace_id
                               for item in status["plugins"]):
                        break
                    await asyncio.sleep(0.05)
            else:
                stopped = await _plugin(case, state="stopped")
                assert stopped["binding"]["enabled"] is False and stopped["instance"] is None
        assert not psutil.pid_exists(blocked_pid)
        assert not psutil.pid_exists(blocked_plugin["instance"]["process_id"])
        assert not gate.exists(), "目录屏障未放开，关闭仍须完成"
        offline_catalog = (await case.client.get("/api/v1/mcp/servers", params={"refresh": "false"})).raise_for_status().json()["data"]
        offline_server = next(server for server in offline_catalog if server["server_id"] == "probe")
        assert offline_server["status"] == "unavailable" and offline_server["tools"] == []
        assert offline_server["connection_ref"] is None
        failed_edit = (await case.client.get(source_path, params={"idempotency_key": blocked_key})).json()["data"]
        assert failed_edit["edit"]["state"] == "recovery_required"
        entries = failed_edit["effective"]["plugins"]["runtime"]["entries"]
        if unbind:
            assert entries == []
        else:
            assert entries[0]["enabled"] is False
        phases_after_close = [json.loads(line) for line in lifecycle.read_text(encoding="utf-8").splitlines()]
        assert not any(row["phase"] == "catalog-released" for row in phases_after_close)
        assert len([row for row in phases_after_close if row["phase"] == "start" and row["version"] == "blocked"]) == 1
        # 关闭态修改同一参数仍只保存，不能创建 client 或进程。
        lifecycle_after_close = lifecycle.read_text(encoding="utf-8")
        close_source["plugins"]["configuration"]["values"]["boxteam.mcp-stdio"]["stdio"]["env"]["MCP_TEST_VERSION"] = "offline"
        await _publish(case, {"plugins": close_source["plugins"]}, f"mcp-plugin-offline-edit-{uuid4().hex}")
        assert lifecycle.read_text(encoding="utf-8") == lifecycle_after_close
        if unbind:
            status = (await case.client.post("/api/gateway/service-plugins/status", json={
                "gateway_id": case.gateway_id,
            })).raise_for_status().json()["data"]
            assert not any(item["binding"]["package_id"] == "boxteam.mcp-stdio"
                           and item["binding"].get("workspace_id") == case.workspace_id
                           for item in status["plugins"]), status
        else:
            assert (await _plugin(case, state="stopped"))["instance"] is None
        (artifacts / "candidate-close.json").write_text(json.dumps({
            "blocked_candidate": failed_edit["edit"], "closed": closed.json(),
            "catalog": offline_catalog, "lifecycle": phases_after_close,
            "stopped_pids": [blocked_pid, blocked_plugin["instance"]["process_id"]],
            "stale_close_status": stale.status_code, "candidate_status": rejected.status_code,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        gate.touch(exist_ok=True)
        await asyncio.gather(blocked, return_exceptions=True)
