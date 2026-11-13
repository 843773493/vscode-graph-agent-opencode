"""真实工作区详情、目标目录绑定与移除保留；SSH 执行／隧道使用 loopback 替身。"""

from __future__ import annotations

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

from app.core.workspace_identity import read_workspace_id, workspace_identity_path
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import TargetGatewayNetwork
from tests.support.paths import chromium_temporary_environment
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)

pytest_plugins = ["tests.integration.gateway.test_target_gateway_routing"]


@pytest.fixture(autouse=True)
def workspace_details_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_WORKSPACE_DETAILS_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


def test_workspace_details_keep_owner_root_and_removed_data(target_gateway_network: TargetGatewayNetwork) -> None:
    network = target_gateway_network
    context = network.context
    local, remote = network.clients
    gateway_a, gateway_b = network.gateway_ids
    topology = json.loads((context.artifacts_dir / "topology.json").read_text())
    registered = local.post("/api/gateway/remote-gateways", json={
        "name": "详情目标 B", "host": "127.0.0.1", "port": 22, "username": "details-integration",
        "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
    }).raise_for_status().json()["data"]
    projected, = [item for item in registered["items"] if item["gateway_id"] == gateway_b]
    connection_id = projected["remote_gateways"][0]["gateway_connection_id"]
    # 本机用户修改目标 Gateway 的正式 JSONC，由原 watcher 发布真实拒绝策略。
    before = remote.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
    configuration = commentjson.loads(network.gateway_config_paths[1].read_text())
    configuration.setdefault("permissions", {})["federation"] = {
        "default_effect": "allow", "rules": [{"capability": "managed_workspace_admin", "effect": "deny"}],
    }
    network.gateway_config_paths[1].write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + "\n")
    deadline = time.monotonic() + 15
    while True:
        current = remote.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
        if current["revision"] != before["revision"] and current["healthy"]:
            assert current["state"] == "active" and not current["restart_required"]
            break
        assert time.monotonic() < deadline, current
        time.sleep(.2)
    workspace_root = prepare_default_test_workspace(
        workspace_root=context.output_root / "registered/workspace",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    workspace_identity_path(workspace_root).unlink(missing_ok=True)
    install_test_workspace_config(workspace_root=workspace_root,
        config_path=network.workspace_roots[0] / ".boxteam/workspace.jsonc",
        schema_path=network.workspace_roots[0] / ".boxteam/workspace_schema.jsonc")
    marker = workspace_root / "details-kept.txt"
    marker.write_text("注销登记保留文件和会话\n")
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "工作区详情验收需要 Chromium"
    assets = Path(os.environ["BOXTEAM_WEB_ASSETS"])
    (context.artifacts_dir / "assets.json").write_text(json.dumps({
        "path": str(assets), "main_sha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest(),
    }) + "\n")
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "300",
        "--", "bun", "tests/integration/clients/web/workspace_details_management.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
        process = subprocess.run(command, cwd=context.project_root, capture_output=True, text=True, check=False,
            timeout=315, env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                "BOXTEAM_BROWSER_ARTIFACTS": str(context.artifacts_dir),
                "BOXTEAM_WORKSPACE_DETAILS_FIXTURE": json.dumps({
                    "baseUrl": str(local.base_url), "gatewayA": gateway_a, "gatewayB": gateway_b,
                    "workspaceId": network.workspace_id, "connectionId": connection_id,
                    "workspaceRoot": str(workspace_root), "remoteRoot": str(network.workspace_roots[1]),
                    "transportBoundary": topology["transport_boundary"],
                })})
    (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout)
    (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr)
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": process.returncode}) + "\n")
    assert process.returncode == 0, f"{process.stdout}\n{process.stderr}\n{context.artifacts_dir}"
    result = json.loads((context.artifacts_dir / "result.json").read_text())
    assert result["phase"] == "completed" and not result["pageErrors"] and not result["unexpectedCalls"]
    denied = result["deniedRegistration"]
    remote_observations = [json.loads(line) for line in network.observation_paths["gateway-b"].read_text().splitlines()]
    rejected = [item for item in remote_observations if item["request_id"] == denied["requestId"]]
    assert len(rejected) == 1
    assert rejected[0]["method"] == "POST" and rejected[0]["path"] == "/api/gateway/federation/managed-workspaces"
    assert rejected[0]["status"] == 403 and rejected[0]["response_request_id"] == denied["requestId"]
    assert denied["detail"] == "工作区创建失败: stage=workspace_create, error=HTTPStatusError"
    limitation = result["observationReleaseLimitation"]
    assert limitation["status"] == 409 and limitation["requestId"]
    assert any(item["kind"] == "proxy_stream" for item in limitation["blockers"])
    assert result["workspaceId"] == read_workspace_id(workspace_root)
    assert marker.read_text() == "注销登记保留文件和会话\n"
    assert result["removedReload"] and result["sessionPreserved"]
