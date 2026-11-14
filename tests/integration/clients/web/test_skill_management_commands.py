"""Skill 来源管理命令沿各自原 owner 打开来源并刷新。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import commentjson
import pytest

from app.core.resource_uri import parse_resource_uri
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import (
    TargetGatewayNetwork,
    _observations,
)
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_network as target_gateway_network,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS

pytest_plugins = ["tests.integration.gateway.test_target_gateway_routing"]


@pytest.fixture(autouse=True)
def skill_management_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_SKILL_MANAGEMENT_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


@pytest.fixture
def skill_management_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> TargetGatewayNetwork:
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", LOCAL_TOKEN_HEADERS["X-Local-Token"])
    return request.getfixturevalue("target_gateway_network")


async def test_skill_management_commands_use_gateway_and_workspace_owners(
    request: pytest.FixtureRequest,
    skill_management_network: TargetGatewayNetwork,
) -> None:
    network = skill_management_network
    client, remote = network.clients
    gateway_id, remote_gateway_id = network.gateway_ids
    workspace_id = network.workspace_id
    context = network.context
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Skill 来源管理验收需要 Chromium"

    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    registration = client.post("/api/gateway/remote-gateways", json={
        "name": "Skill 来源浏览远端 B", "host": "127.0.0.1", "port": 22,
        "username": "skill-management", "private_key_path": topology["private_key"],
        "remote_gateway_port": topology["ports"][3],
    }).raise_for_status().json()["data"]
    projected, = [item for item in registration["items"] if item["gateway_id"] == remote_gateway_id]
    connection_id = projected["remote_gateways"][0]["gateway_connection_id"]
    assert connection_id

    source_path = "/api/gateway/config/sources/gateway_mutable_override"
    source_document = remote.get(source_path).raise_for_status().json()["data"]
    configuration = commentjson.loads(source_document["jsonc"])
    configuration.setdefault("permissions", {})["federation"] = {
        "default_effect": "deny",
        "rules": [{"capability": "read", "effect": "allow",
                   "principal_ref": principal_ref_for_gateway(gateway_id)}],
    }
    policy = remote.put(source_path, json={
        "gateway_id": remote_gateway_id, "source_key": "gateway_mutable_override",
        "jsonc": json.dumps(configuration, ensure_ascii=False, indent=2) + "\n",
        "base_file_digest": source_document["file_digest"],
        "base_layer_revision": source_document["layer_revision"],
        "base_layer_digest": source_document["layer_digest"],
        "expected_active_revision": source_document["active_revision"],
        "expected_active_digest": source_document["active_digest"],
        "idempotency_key": "skill-management-remote-read-policy",
    }).raise_for_status().json()["data"]
    assert policy["edit"]["state"] == "active", policy
    previous_revision = source_document["active_revision"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = remote.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
        if status["revision"] != previous_revision and status["healthy"] is True:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("远端 Gateway B 未接受 Skill 来源读取授权")

    remote_skill_name = "remote-skill-management-proof"
    remote_skill_marker = "REMOTE_GATEWAY_SKILL_SOURCE_BODY_7f3c"
    remote_skill_root = network.gateway_homes[1] / "skills" / remote_skill_name
    remote_skill_root.mkdir(parents=True)
    (remote_skill_root / "SKILL.md").write_text(
        f"---\nname: {remote_skill_name}\ndescription: 远端浏览验收来源\n---\n{remote_skill_marker}\n",
        encoding="utf-8",
    )
    setup_refresh = remote.post("/api/gateway/skill-sources/refresh", headers={
        "X-Request-ID": "skill-management-setup-refresh-remote-b",
    }).raise_for_status()
    assert setup_refresh.headers["X-Request-ID"] == setup_refresh.json()["request_id"]
    remote_sources_path = f"/api/gateway/skill-sources/{remote_gateway_id}/workspaces/{workspace_id}"
    remote_sources_response = remote.get(remote_sources_path, headers={
        "X-Request-ID": "skill-management-setup-read-remote-b",
    }).raise_for_status()
    assert remote_sources_response.headers["X-Request-ID"] == remote_sources_response.json()["request_id"]
    remote_sources = remote_sources_response.json()["data"]
    remote_skill, = [item for item in remote_sources["entries"] if item["name"] == remote_skill_name]
    assert remote_skill["display_uri"] == (
        f"boxteam://gateway/{remote_gateway_id}/resources/skills/{remote_skill_name}/SKILL.md"
    )

    workspaces = client.get("/api/gateway/workspaces?check_health=false").raise_for_status().json()["data"]["items"]
    local_workspaces = [item for item in workspaces if item["gateway_id"] == gateway_id and item["status"] == "ready"]
    remote_workspaces = [item for item in workspaces if item["gateway_id"] == remote_gateway_id and item["status"] == "ready"]
    workspace, = local_workspaces
    remote_workspace, = remote_workspaces
    assert workspace["workspace_id"] == remote_workspace["workspace_id"] == workspace_id
    health = client.get("/api/gateway/health").raise_for_status().json()["data"]
    assert health["gateway_id"] == gateway_id

    response = client.get("/api/gateway/config/sources")
    response.raise_for_status()
    gateway_sources = response.json()["data"]["sources"]
    response = client.get("/api/v1/config/sources", headers={
        TARGET_GATEWAY_ID_HEADER: gateway_id,
        "X-BoxTeam-Workspace-Id": workspace_id,
    })
    response.raise_for_status()
    workspace_sources = response.json()["data"]["sources"]
    assert gateway_sources and workspace_sources

    assets = Path(os.environ["BOXTEAM_WEB_ASSETS"]).resolve(strict=True)
    main_asset = assets / "out/main.js"
    (context.artifacts_dir / "skill-management-assets.json").write_text(json.dumps({
        "path": str(assets), "main_sha256": hashlib.sha256(main_asset.read_bytes()).hexdigest(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result_path = context.artifacts_dir / "skill-management-result.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
        "--timeout-seconds", "180", "--", "node", "tests/integration/clients/web/skill_management.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_SKILL_MANAGEMENT_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_SKILL_MANAGEMENT_RESULT": str(result_path), "BOXTEAM_SKILL_MANAGEMENT_GATEWAY_ID": gateway_id,
                 "BOXTEAM_SKILL_MANAGEMENT_REMOTE_GATEWAY_ID": remote_gateway_id,
                 "BOXTEAM_SKILL_MANAGEMENT_WORKSPACE_ID": workspace_id,
                 "BOXTEAM_SKILL_MANAGEMENT_WORKSPACE_NAME": workspace["name"],
                 "BOXTEAM_SKILL_MANAGEMENT_REMOTE_SKILL_NAME": remote_skill_name,
                 "BOXTEAM_SKILL_MANAGEMENT_REMOTE_SKILL_MARKER": remote_skill_marker,
                 "BOXTEAM_SKILL_MANAGEMENT_LOCAL_WORKSPACE_COUNT": str(len(local_workspaces)),
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=195, check=False)
    (context.artifacts_dir / "skill-management-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n")
    (context.artifacts_dir / "skill-management.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "skill-management.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    observed = json.loads(result_path.read_text(encoding="utf-8"))
    assert observed["phase"] == "completed"
    assert observed["errors"] == []
    assert len(observed["initialization"]) >= 3
    for route in ("/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"):
        responses = [item for item in observed["initialization"] if item["path"] == route]
        assert responses, route
        assert all(item["status"] == 200 for item in responses)
        assert all(item["requestId"] and item["requestId"] == item["envelopeRequestId"] for item in responses)
    registry = next(item["data"] for item in observed["initialization"] if item["path"] == "/api/gateway/workspaces")
    assert {item["gateway_id"] for item in registry["items"]} == {gateway_id, remote_gateway_id}
    assert observed["remoteBrowse"]["owner"] == {"gateway_id": remote_gateway_id, "workspace_id": workspace_id}
    assert remote_gateway_id in observed["remoteBrowse"]["pickerRow"]

    remote_metadata_responses = [item for item in observed["requests"] if item["path"] == "/api/v1/resources/gateway-skills"
                                 and item["headers"].get("x-boxteam-target-gateway-id") == remote_gateway_id]
    assert remote_metadata_responses
    assert all(item["method"] == "GET" for item in remote_metadata_responses)
    assert all(item["headers"]["x-boxteam-workspace-id"] == workspace_id for item in remote_metadata_responses)
    assert all(item["status"] == 200 for item in remote_metadata_responses)
    assert all(item["requestId"] and item["requestId"] == item["envelopeRequestId"]
               for item in remote_metadata_responses)
    remote_metadata = remote_metadata_responses[-1]
    remote_skill, = [item for item in remote_metadata["data"]["entries"] if item["name"] == remote_skill_name]
    assert remote_metadata["data"]["gateway_id"] == remote_gateway_id
    assert remote_metadata["data"]["workspace_id"] == workspace_id
    assert remote_skill["display_uri"] == f"boxteam://gateway/{remote_gateway_id}/resources/skills/{remote_skill_name}/SKILL.md"
    remote_navigation_responses = [item for item in observed["requests"]
                                   if item["path"] == "/api/v1/resources/navigation"
                                   and item["headers"].get("x-boxteam-target-gateway-id") == remote_gateway_id]
    assert remote_navigation_responses
    assert all(item["method"] == "POST" for item in remote_navigation_responses)
    assert all(parse_resource_uri(item["body"]["uri"], local_gateway_id=remote_gateway_id).display_uri
               == remote_skill["display_uri"] for item in remote_navigation_responses)
    assert all(item["body"]["operation"] == "read" for item in remote_navigation_responses)
    assert all(item["headers"]["x-boxteam-workspace-id"] == workspace_id for item in remote_navigation_responses)
    assert all(item["status"] == 200 for item in remote_navigation_responses)
    assert all(item["requestId"] and item["requestId"] == item["envelopeRequestId"]
               for item in remote_navigation_responses)

    for browser_request in remote_metadata_responses:
        forwarded = _observations(network.observation_paths["gateway-b"], browser_request["requestId"])
        assert any(item["path"] == (
            f"/api/gateway/skill-source-connections/{remote_gateway_id}/workspaces/{workspace_id}/metadata")
            and item["method"] == "GET" and item["status"] == 200
            and item["response_request_id"] == browser_request["requestId"] for item in forwarded)
    remote_attempt = observed["remoteRefreshAttempt"]
    assert remote_attempt["available"] is False
    assert len(remote_attempt["offeredTargets"]) == len(local_workspaces)
    assert len(registry["items"]) > len(remote_attempt["offeredTargets"])
    assert remote_attempt["before"] == remote_attempt["after"] == {"gateway": 0, "workspace": 0, "remote": 0}

    gateway_refresh = next(item for item in observed["requests"] if item["path"] == "/api/gateway/skill-sources/refresh")
    workspace_refresh = next(item for item in observed["requests"] if item["path"] == "/api/v1/resources/skill-sources/refresh")
    assert gateway_refresh["method"] == "POST"
    assert gateway_refresh["status"] == 200
    assert gateway_refresh["requestId"] and gateway_refresh["requestId"] == gateway_refresh["envelopeRequestId"]
    assert gateway_refresh["headers"].get("x-boxteam-workspace-id") is None
    assert workspace_refresh["method"] == "POST"
    assert workspace_refresh["status"] == 200
    assert workspace_refresh["requestId"] and workspace_refresh["requestId"] == workspace_refresh["envelopeRequestId"]
    assert workspace_refresh["headers"]["x-boxteam-target-gateway-id"] == gateway_id
    assert workspace_refresh["headers"]["x-boxteam-workspace-id"] == workspace_id
    assert len([item for item in observed["requests"] if item["method"] == "POST" and item["path"] == gateway_refresh["path"]]) == 1
    assert len([item for item in observed["requests"] if item["method"] == "POST" and item["path"] == workspace_refresh["path"]]) == 1
    assert any(item["path"] == "/api/gateway/config/sources" for item in observed["requests"])
    assert any(item["path"].startswith("/api/v1/config/sources/") for item in observed["requests"])
