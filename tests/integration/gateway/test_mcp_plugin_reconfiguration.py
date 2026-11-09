"""真实 Gateway 插件参数候选、stdio 替换、目录发布与失败修复。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

import commentjson
import portalocker
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


async def test_parameter_reconfiguration_orders_plugin_handoff_before_mcp_promotion(
    mcp_plugin_case: McpPluginCase,
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

    archive = _package(artifacts / "mcp-bridge.zip")
    response = await case.client.post("/api/gateway/service-plugins/install", json={
        "gateway_id": case.gateway_id, "path": str(archive),
        "expected_digest": hashlib.sha256(archive.read_bytes()).hexdigest(),
    })
    response.raise_for_status()

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
    failed_catalog_response.raise_for_status()
    failed_catalog = next(item for item in failed_catalog_response.json()["data"] if item["server_id"] == "probe")
    assert failed_catalog["connection_ref"] == after_catalog["connection_ref"]
    failed_phases = [json.loads(line) for line in lifecycle.read_text(encoding="utf-8").splitlines()]
    failed_effects = [effect for row in failed_phases[phase_count:] for effect in row["effects"]]
    assert any(effect.get("resource") == "workspace-plugin-runtime"
               and effect.get("action") == "candidate-start-intent" for effect in failed_effects)

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
