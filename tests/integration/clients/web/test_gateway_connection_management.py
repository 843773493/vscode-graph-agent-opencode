"""连接管理沿 loopback 配对装具；Ports 场景使用真实 localhost SSH。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _prepare_workspace,
)
from tests.integration.gateway.test_target_gateway_routing import TargetGatewayNetwork
from tests.support.communication.federation import open_communication_triad
from tests.support.http_server import running_http_server
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_ports_for_file

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


@pytest.mark.asyncio
async def test_ports_create_open_and_stop_real_ssh_forward(
    request: pytest.FixtureRequest,
) -> None:
    assets = Path(os.environ["BOXTEAM_WEB_ASSETS"])
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_ports_for_file(Path(request.node.path))
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Ports 产品验收需要真实 Chromium"
    landing = context.workspace_root / "port-landing"
    landing.mkdir(parents=True, exist_ok=True)
    marker = "Ports 管理入口真实 SSH 转发"
    (landing / "index.html").write_text(
        f'<!doctype html><meta charset="utf-8"><title>Ports Ready</title><h1>{marker}</h1>', encoding="utf-8",
    )
    ports.port(61)
    ports.port(62)
    server = ThreadingHTTPServer(("127.0.0.1", ports.port(61)), partial(SimpleHTTPRequestHandler, directory=str(landing)))
    with running_http_server(server):
        async with open_communication_triad(
            context, _prepare_workspace, same_backend_routes=True, ports=ports,
            gateway_environment={"BOXTEAM_WEB_ASSETS": str(assets)},
        ) as nodes:
            source, target = nodes["a"], nodes["b"]
            assert source.gateway is not None
            command = [sys.executable, str(context.project_root / "app/runtime/posix_job.py"),
                       "--memory-limit-kb", "4194304", "--timeout-seconds", "120", "--",
                       "bun", "tests/integration/clients/web/port_management.mjs"]
            with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium") as temporary:
                completed = await asyncio.to_thread(
                    subprocess.run, command, cwd=context.project_root, capture_output=True,
                    text=True, check=False, timeout=135, env={**os.environ, **temporary,
                        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                        "BOXTEAM_PORT_MANAGEMENT_ARTIFACTS": str(context.artifacts_dir),
                        "BOXTEAM_PORT_MANAGEMENT_FIXTURE": json.dumps({
                            "baseUrl": f"http://127.0.0.1:{source.gateway.port}",
                            "gatewayId": target.gateway_id, "workspaceId": target.workspace_id,
                            "remotePort": ports.port(61), "localPort": ports.port(62), "marker": marker,
                        })},
                )
            (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
            (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
            (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
                "command": command, "exit_code": completed.returncode,
                "assets": str(assets),
                "main_sha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest(),
                "boundary": "真实 Workbench／双 Gateway／Backend／localhost SSH 与 HTTP 服务，无业务响应替身",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{context.artifacts_dir}"
            observed = json.loads((context.artifacts_dir / "result.json").read_text(encoding="utf-8"))
            assert observed["phase"] == "completed" and observed["pageErrors"] == []
            assert observed["draftPreservedAfterOwnerRefresh"] is True
            assert observed["openedThroughSsh"] and observed["stoppedForwardOnly"]
            # 同一装具持有真实 SSH，补验远端监听及两段中继，不用模拟命令代替数据传输。
            assert source.client is not None
            hosts = (await source.client.get("/api/gateway/port-forwards/hosts")).raise_for_status().json()["data"]["items"]
            ssh_hosts = [item["host_id"] for item in hosts if item["host_id"] == "rgw_c3_b"]
            assert len(ssh_hosts) == 1
            directions = []
            for destination_host in ["local", ssh_hosts[0]]:
                response = (await source.client.post("/api/gateway/port-forwards", json={
                    "listener": {"host_id": ssh_hosts[0], "port": ports.port(62)},
                    "destination": {"host_id": destination_host, "port": ports.port(61)},
                })).raise_for_status()
                forward = response.json()["data"]["items"][0]
                assert forward["status"] == "active", forward
                try:
                    async with httpx.AsyncClient() as client:
                        transferred = await client.get(f"http://127.0.0.1:{ports.port(62)}")
                        assert marker in transferred.raise_for_status().text
                    directions.append({"listener": ssh_hosts[0], "destination": destination_host,
                                       "request_id": response.json()["request_id"], "status": forward["status"]})
                finally:
                    (await source.client.delete(f"/api/gateway/port-forwards/{forward['forward_id']}")).raise_for_status()
            (context.artifacts_dir / "ssh-directions.json").write_text(json.dumps(directions, indent=2) + "\n")
