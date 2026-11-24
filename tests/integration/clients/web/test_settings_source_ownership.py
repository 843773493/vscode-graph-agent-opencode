"""真实双 Gateway／Workbench 验证来源绑定与共享来源旧草稿冲突。"""

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

from app.gateway.federation.ports import principal_ref_for_gateway
from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_target_gateway_routing import TargetGatewayNetwork
from tests.support.paths import chromium_temporary_environment

pytest_plugins = ["tests.integration.gateway.test_target_gateway_routing"]


@pytest.fixture(autouse=True)
def ownership_scene(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
    integration_workspace_config_path: str,
) -> Path:
    """启动前只准备本场景输入；两工作区由原网络装具复制同一显式父引用。"""
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    assets = Path(os.environ["BOXTEAM_SETTINGS_OWNER_WEB_ASSETS"]).resolve(strict=True)
    assert (assets / "version.json").is_file()
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    shared = context.runtime_root_for_node("shared-source") / "shared-parent.jsonc"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(json.dumps({"config_version": 4, "extends": "user:workspace_local", "settings": {}}) + "\n", encoding="utf-8")
    source = Path(integration_workspace_config_path)
    document = commentjson.loads(source.read_text(encoding="utf-8"))
    document["extends"] = str(shared)
    source.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return shared


def test_settings_remote_owner_and_shared_source_stale_draft(
    target_gateway_network: TargetGatewayNetwork, ownership_scene: Path,
) -> None:
    network, shared = target_gateway_network, ownership_scene
    context = network.context
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "来源归属验收需要真实 Chromium"
    gateway_a, gateway_b = network.gateway_ids
    # 配对不自动授予配置读写权；只在本场景隔离 Home 显式授权，等待原 watcher 激活。
    for native, policy_path, peer_id in zip(network.clients, network.gateway_config_paths, (gateway_b, gateway_a), strict=True):
        before_policy = native.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
        gateway_config = commentjson.loads(policy_path.read_text(encoding="utf-8"))
        gateway_config.setdefault("settings", {}).setdefault("gateway", {}).setdefault("permissions", {})["federation"] = {
            "default_effect": "allow", "rules": [{"capability": capability, "effect": "allow",
                "principal_ref": principal_ref_for_gateway(peer_id)} for capability in ("configuration_read", "configuration_write")],
        }
        policy_path.write_text(json.dumps(gateway_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            policy = native.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
            if policy["revision"] != before_policy["revision"] and policy["healthy"] is True:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"测试配置读写授权未激活：{policy}")
    initial_documents = []
    for client, workspace in zip(network.clients, network.workspace_roots, strict=True):
        assert commentjson.loads((workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))["extends"] == str(shared)
        directory = client.get("/api/v1/config/sources").raise_for_status().json()["data"]
        assert {"user", "user_local", "workspace", "inline", "source"} <= {item["layer"] for item in directory["sources"]}
        entry, = [item for item in directory["sources"] if item["layer"] == "source"]
        document = client.get(f"/api/v1/config/sources/{entry['source_key']}").raise_for_status().json()["data"]
        assert document["jsonc"] == shared.read_text(encoding="utf-8")
        initial_documents.append(document)
    assert initial_documents[0]["source_key"] == initial_documents[1]["source_key"]
    assert initial_documents[0]["file_digest"] == initial_documents[1]["file_digest"]
    (context.artifacts_dir / "shared-source-before-browser.json").write_text(json.dumps(initial_documents,
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    remote_name = "来源归属 Gateway B"
    response = network.clients[0].post("/api/gateway/remote-gateways", json={
        "name": remote_name, "host": "127.0.0.1", "port": 22, "username": "source-owner",
        "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
    })
    assert response.status_code == 200, response.text
    target, = [item for item in response.json()["data"]["items"] if item["gateway_id"] == gateway_b]
    connection, = target["remote_gateways"]
    assets = Path(os.environ["BOXTEAM_WEB_ASSETS"])
    main_digest = hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest()
    result_path = context.artifacts_dir / "source-ownership-result.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180",
        "--", "bun", "tests/integration/clients/web/settings_source_ownership.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
        run = subprocess.run(command, cwd=context.project_root, capture_output=True, text=True, check=False,
            timeout=195, env=os.environ | temporary | {
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                "BOXTEAM_SOURCE_OWNERSHIP_FIXTURE": json.dumps({"baseUrl": str(network.clients[0].base_url),
                    "gatewayA": gateway_a, "gatewayB": gateway_b, "workspaceId": network.workspace_id,
                    "remoteName": remote_name, "connectionId": connection["gateway_connection_id"],
                    "artifacts": str(context.artifacts_dir), "resultPath": str(result_path)}),
            })
    (context.artifacts_dir / "browser.stdout.raw").write_text(run.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(run.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
        "exit_code": run.returncode, "assets": str(assets), "main_sha256": main_digest,
        "boundary": topology["transport_boundary"], "shared_source": str(shared)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest() == main_digest
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}\n{result_path}"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["phase"] == "complete" and result["pageErrors"] == []
    assert result["remoteOwner"]["gateway_id"] == gateway_b
    assert result["remoteOwner"]["navigation_gateway_id"] == gateway_a
    assert result["conflict"]["status"] == 409 and result["conflict"]["draft_preserved"]
    assert shared.read_text(encoding="utf-8") == result["conflict"]["committed_jsonc"]
