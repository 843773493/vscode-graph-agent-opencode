"""完整 Workbench 验收连接管理；SSH 执行与隧道明确复用 loopback 替身。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import commentjson
import pytest

from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import TargetGatewayNetwork
from tests.support.paths import chromium_temporary_environment

pytest_plugins = ["tests.integration.gateway.test_target_gateway_routing"]


@pytest.fixture(autouse=True)
def connection_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_GATEWAY_MANAGEMENT_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


def test_connections_keep_original_source_and_remote_data(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    context = network.context
    local, remote = network.clients
    gateway_a, gateway_b = network.gateway_ids
    topology = json.loads((context.artifacts_dir / "topology.json").read_text())
    connection_id = "connection_config_management"
    source_path = "/api/gateway/config/sources/gateway_mutable_override"
    document = local.get(source_path).raise_for_status().json()["data"]
    configuration = commentjson.loads(document["jsonc"])
    configuration["workspaces"] = [{
        "kind": "remote_gateway", "connection_id": connection_id, "name": "配置连接 B",
        "host": "127.0.0.1", "port": 22, "username": "connection-config",
        "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
    }]
    request = {
        "gateway_id": gateway_a, "source_key": "gateway_mutable_override",
        "jsonc": json.dumps(configuration, ensure_ascii=False, indent=2) + "\n",
        "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"], "idempotency_key": "connection-config-management",
    }
    preview = local.post(source_path + "/preflight", json=request).raise_for_status()
    saved = local.put(source_path, json=request).raise_for_status()
    pending = saved.json()["data"]
    assert pending["edit"]["source_saved"] and pending["edit"]["state"] == "pending_restart", pending
    # 准备配置连接只走原来源 writer；原 handoff 消费权威 restart intent，不补写运行事实。
    database_path = network.gateway_homes[0] / "state/gateway/gateway.sqlite"
    with closing(sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)) as database:
        intent = database.execute(
            "SELECT target_generation, fencing_token FROM gateway_restart_intent WHERE candidate_ref = ?",
            (pending["status"]["candidate_ref"],),
        ).fetchone()
    assert intent is not None
    local = network.handoff_gateway({
        "BOXTEAM_CONFIG_CANDIDATE_REF": pending["status"]["candidate_ref"],
        "BOXTEAM_CONFIG_GENERATION": intent[0], "BOXTEAM_CONFIG_FENCING_TOKEN": intent[1],
    })
    network.finish_gateway_handoff()
    active = local.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
    assert active["state"] == "active" and active["restart_required"] is False
    (context.artifacts_dir / "config-preparation.json").write_text(json.dumps({
        "preflight_request_id": preview.json()["request_id"], "save_request_id": saved.json()["request_id"],
        "connection_id": connection_id, "active_revision": active["active_revision"],
        "boundary": topology["transport_boundary"],
    }, ensure_ascii=False, indent=2) + "\n")

    marker = network.workspace_roots[1] / "connection-kept.txt"
    marker.write_text("移除连接必须保留远端文件\n")
    created = remote.post("/api/v1/sessions", json={"title": "连接解绑保留的会话"}).raise_for_status().json()["data"]
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "连接管理验收需要 Chromium"
    assets = Path(os.environ["BOXTEAM_WEB_ASSETS"])
    (context.artifacts_dir / "assets.json").write_text(json.dumps({
        "path": str(assets), "main_sha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest(),
    }) + "\n")
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180",
        "--", "bun", "tests/integration/clients/web/gateway_connection_management.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
        process = subprocess.run(command, cwd=context.project_root, capture_output=True, text=True, check=False,
            timeout=195, env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                "BOXTEAM_BROWSER_ARTIFACTS": str(context.artifacts_dir),
                "BOXTEAM_CONNECTION_MANAGEMENT_FIXTURE": json.dumps({
                    "baseUrl": str(local.base_url), "gatewayA": gateway_a, "gatewayB": gateway_b,
                    "workspaceId": network.workspace_id, "configConnectionId": connection_id,
                    "privateKeyPath": topology["private_key"], "remoteGatewayPort": topology["ports"][3],
                })})
    (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout)
    (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr)
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": process.returncode,
    }) + "\n")
    assert process.returncode == 0, f"{process.stdout}\n{process.stderr}\n{context.artifacts_dir}"
    observed = json.loads((context.artifacts_dir / "result.json").read_text())
    assert observed["phase"] == "completed"
    assert observed["pageErrors"] == observed["unexpectedCalls"] == []
    assert observed["route_stubs"] == []
    assert marker.read_text() == "移除连接必须保留远端文件\n"
    assert remote.get(f"/api/v1/sessions/{created['session_id']}").raise_for_status().json()["data"]["session_id"] == created["session_id"]
    remaining = local.get("/api/gateway/management/gateways").raise_for_status().json()["data"]["items"]
    assert [item["gateway_connection_id"] for item in remaining if item["connection_kind"] == "remote_gateway"] == [connection_id]
