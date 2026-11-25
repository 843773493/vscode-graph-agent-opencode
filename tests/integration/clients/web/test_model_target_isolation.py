"""同 UUID、同名模型在双 Gateway 中的真实偏好、设置及凭据归属。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import commentjson
import pytest

from app.gateway.federation.ports import principal_ref_for_gateway
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.backend.config.test_llm_discovery import Catalog
from tests.integration.backend.config.test_llm_discovery import (
    catalog as catalog,  # noqa: PLC0414
)
from tests.integration.clients.web.test_model_connection_probe import (
    probe_web_assets as probe_web_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import TargetGatewayNetwork
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_network as target_gateway_network,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_port as target_gateway_port,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


@pytest.fixture(autouse=True)
def model_target_configuration(
    catalog: Catalog, probe_web_assets: Path, integration_workspace_config_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    source = Path(integration_workspace_config_path)
    configuration = commentjson.loads(source.read_text(encoding="utf-8"))
    secret_id = f"sec_{uuid4().hex}"
    workspace = configuration["settings"]["workspace"]
    workspace["llm"]["providers"]["primary"] = {
        "name": "同名模型服务",
        "config_ref": "#/profiles/provider.primary",
        "models": {"default": {"model": "same-main", "config_ref": "context:provider/primary"},
                   "backup": {"model": "same-backup", "config_ref": "context:provider/primary"}},
    }
    configuration["profiles"]["provider.primary"]["settings"] = {
        "network": {"proxy": {"mode": "direct"}},
        "model_options": {"endpoint": catalog.endpoint, "api_key": f"secret:{secret_id}",
            "custom_llm_provider": "openai",
            "api_mode": {"protocol": "chat_completions", "model_info": {"supports_function_calling": True}}},
    }
    for agent in workspace["agents"].values():
        agent["model"] = {"primary": {"provider_id": "primary", "model_id": "default"},
            "fallbacks": [{"provider_id": "primary", "model_id": "backup"}]}
    source.write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(probe_web_assets))
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", LOCAL_TOKEN_HEADERS["X-Local-Token"])
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    return secret_id


async def test_same_model_names_keep_native_target_preferences_and_credentials(
    target_gateway_network: TargetGatewayNetwork, model_target_configuration: str, catalog: Catalog,
) -> None:
    network = target_gateway_network
    context = network.context
    hub = network.clients[0]
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    registered = await asyncio.to_thread(hub.post, "/api/gateway/remote-gateways", json={
        "name": "同名模型远端", "host": "127.0.0.1", "port": 22, "username": "model-isolation",
        "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3],
    })
    assert registered.status_code == 200, registered.text
    # 远端管理需由 B 的用户配置显式授权 A，等待原 watcher 发布后再使用。
    native = network.clients[1]
    before = (await asyncio.to_thread(native.get, "/api/gateway/config/reload-status")).json()["data"]
    config_path = network.gateway_config_paths[1]
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("settings", {}).setdefault("gateway", {}).setdefault("permissions", {})["federation"] = {"default_effect": "allow", "rules": [
        {"capability": capability, "effect": "allow", "principal_ref": principal_ref_for_gateway(network.gateway_ids[0])}
        for capability in ("configuration_read", "configuration_write")
    ]}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    deadline = time.monotonic() + 15
    while True:
        response = await asyncio.to_thread(native.get, "/api/gateway/config/reload-status")
        assert response.status_code == 200, response.text
        current = response.json()["data"]
        if current["revision"] != before["revision"] and current["healthy"] is True:
            assert current["state"] == "active" and current["restart_required"] is False
            break
        assert time.monotonic() < deadline, current
        await asyncio.sleep(0.2)
    values = [f"model-target-{index}-{uuid4().hex}" for index in range(2)]
    for gateway_id, value in zip(network.gateway_ids, values, strict=True):
        created = await asyncio.to_thread(hub.post, "/api/gateway/secrets",
            headers={"X-BoxTeam-Target-Gateway-Id": gateway_id},
            json={"id": model_target_configuration, "name": "同名共享密钥", "value": value})
        assert created.status_code == 200, created.text
        receipt = created.json()
        assert receipt["data"]["gateway_id"] == gateway_id
        assert receipt["request_id"] == created.headers["X-Request-ID"]
    originals = [(workspace / ".boxteam/workspace.jsonc").read_bytes() for workspace in network.workspace_roots]
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "双 Gateway 模型验收需要 Chromium"
    command = ["bun", "tests/integration/clients/web/model_target_isolation.mjs"]
    fixture = {"baseUrl": str(hub.base_url).rstrip("/"), "gatewayIds": network.gateway_ids,
        "workspaceId": network.workspace_id, "artifacts": str(context.artifacts_dir)}
    with chromium_temporary_environment(context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
        process = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env=os.environ | temporary | {"BOXTEAM_MODEL_TARGET_FIXTURE": json.dumps(fixture),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=150, check=False)
    (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": process.returncode, "assets": os.environ["BOXTEAM_WEB_ASSETS"],
    }) + "\n", encoding="utf-8")
    assert process.returncode == 0, f"{process.stdout}\n{process.stderr}\n{context.artifacts_dir}"
    observed = json.loads((context.artifacts_dir / "result.json").read_text(encoding="utf-8"))
    assert observed["phase"] == "complete" and observed["pageErrors"] == []
    assert len(catalog.model_requests) == 2 and all(body["model"] == "same-backup" for body in catalog.model_requests)
    assert [headers["authorization"] for _, headers in catalog.requests] == [f"Bearer {values[1]}", f"Bearer {values[0]}"]
    assert all(path == "/v1/chat/completions" for path, _ in catalog.requests), "偏好和设置打开不能触发目录发现"
    observations = {role: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for role, path in network.observation_paths.items()}
    for receipt in observed["probes"]:
        index = network.gateway_ids.index(receipt["gatewayId"])
        request_id = receipt["requestId"]
        delivered = [item for item in observations[f"backend-{'ab'[index]}"] if item["request_id"] == request_id]
        assert len(delivered) == 1 and delivered[0]["response_request_id"] == request_id
        assert delivered[0]["workspace_id"] == network.workspace_id and delivered[0]["status"] == 200
        assert not any(item["request_id"] == request_id for item in observations[f"backend-{'ba'[index]}"])
    assert originals == [(workspace / ".boxteam/workspace.jsonc").read_bytes() for workspace in network.workspace_roots]
    (context.artifacts_dir / "credential-owner.json").write_text(json.dumps({
        "gateway_ids": network.gateway_ids, "shared_secret_id": model_target_configuration,
        "native_credentials_observed": True, "model_requests": len(catalog.model_requests),
    }) + "\n", encoding="utf-8")
