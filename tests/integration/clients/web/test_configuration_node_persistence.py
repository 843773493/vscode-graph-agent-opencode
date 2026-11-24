"""受控模型边界下，真实 Workbench 配置节点保存、隔离与冷启动读回。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import commentjson
import httpx
import pytest

from configs.installer import install_user_configuration
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.backend.config.test_llm_discovery import Catalog
from tests.integration.backend.config.test_llm_discovery import (
    catalog as catalog,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_ports_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)


@pytest.fixture
def node_web_assets(request: pytest.FixtureRequest) -> Path:
    prepared = os.getenv("BOXTEAM_NODE_PERSISTENCE_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file()
    return assets


async def test_configuration_nodes_survive_restart_without_changing_shared_originals(
    request: pytest.FixtureRequest, catalog: Catalog, node_web_assets: Path,
    integration_workspace_root_path: str, integration_workspace_config_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    source = Path(integration_workspace_config_path)
    external = source.with_name("node-shared.jsonc")
    external.write_text(json.dumps({
        "config_version": 4, "settings": {}, "profiles": {
            "provider": {"extends": "builtin:workspace#/profiles/provider.primary", "settings": {
                "model_options": {"endpoint": catalog.endpoint, "api_key": "", "custom_llm_provider": "openai",
                    "api_mode": {"protocol": "chat_completions", "supports_reasoning": {},
                        "model_info": {"supports_reasoning": False}}},
                "network": {"proxy": {"mode": "direct"}},
            }},
            "model": {"extends": "#/profiles/provider", "settings": {
                "model_options": {"request_options": {"parameters": {"temperature": 0.25}}},
            }},
        },
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shared_original = external.read_bytes()
    config = commentjson.loads(source.read_text(encoding="utf-8"))
    business = config["settings"]["workspace"]
    business["llm"]["providers"] = {
        "primary": {"name": "节点持久化原件", "config_ref": "./node-shared.jsonc#/profiles/provider", "models": {
            "default": {"model": "upstream-main", "config_ref": "./node-shared.jsonc#/profiles/model"},
            "sibling": {"model": "upstream-sibling", "config_ref": "./node-shared.jsonc#/profiles/model"},
        }},
        "peer": {"name": "共享节点同伴", "config_ref": "./node-shared.jsonc#/profiles/provider", "models": {}},
    }
    for agent in business["agents"].values():
        agent["model"] = {"primary": {"provider_id": "primary", "model_id": "default"}, "fallbacks": []}
    ports = integration_ports_for_file(Path(request.node.path))
    backend_port, gateway_port = ports.port(0), ports.port(20)
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    business.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = gateway_url
    source.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    home = context.boxteam_home_for_node(request.node.nodeid)
    install_user_configuration(config_root=home / "config", profile="default", project_root=context.project_root)
    gateway_source = home / "config/gateway.jsonc"
    gateway_config = commentjson.loads(gateway_source.read_text(encoding="utf-8"))
    gateway_config.setdefault("settings", {}).setdefault("gateway", {}).setdefault("plugins", {})["workspace_defaults"] = {"entries": []}
    gateway_source.write_text(json.dumps(gateway_config), encoding="utf-8")
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "配置节点验收需要 Chromium"
    (context.artifacts_dir / "assets.json").write_text(json.dumps({
        "path": str(node_web_assets), "main_sha256": hashlib.sha256((node_web_assets / "out/main.js").read_bytes()).hexdigest(),
    }) + "\n", encoding="utf-8")

    def start_gateway():
        return start_gateway_process(workspace_root=workspace, default_backend_url=None, port=gateway_port,
            boxteam_home=home, artifacts_dir=context.artifacts_dir / "gateway",
            extra_env={"BOXTEAM_WEB_ASSETS": str(node_web_assets), "BOXTEAM_DEFAULT_BACKEND_PORT": str(backend_port),
                "LITELLM_LOCAL_MODEL_COST_MAP": "true"})

    gateway = await asyncio.to_thread(start_gateway)
    try:
        async with httpx.AsyncClient(base_url=gateway_url, headers=LOCAL_TOKEN_HEADERS, trust_env=False, timeout=90) as client:
            response = await client.get("/api/gateway/workspaces")
            response.raise_for_status()
            owner = response.json()["data"]["active_workspace"]
        fixture = {"baseUrl": gateway_url, "gatewayId": owner["gateway_id"], "workspaceId": owner["workspace_id"],
            "externalProvider": str(external.resolve()) + "#/profiles/provider",
            "externalModel": str(external.resolve()) + "#/profiles/model", "endpoint": catalog.endpoint + "/private"}
        for phase in ("save", "read"):
            command = ["timeout", "180s", "bash", "-c", 'ulimit -d 4194304; exec "$@"', "bash",
                "bun", "tests/integration/clients/web/configuration_node_persistence.mjs"]
            with chromium_temporary_environment(context.runtime_root_for_node(phase) / "chromium-tmp") as temporary:
                completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
                    capture_output=True, text=True, check=False, timeout=195,
                    env=os.environ | temporary | {"PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                        "BOXTEAM_BROWSER_ARTIFACTS": str(context.artifacts_dir), "BOXTEAM_NODE_FIXTURE": json.dumps(fixture | {"phase": phase})})
            (context.artifacts_dir / f"{phase}.stdout.raw").write_text(completed.stdout, encoding="utf-8")
            (context.artifacts_dir / f"{phase}.stderr.raw").write_text(completed.stderr, encoding="utf-8")
            (context.artifacts_dir / f"{phase}-process.json").write_text(json.dumps({
                "command": command, "exit_code": completed.returncode,
            }) + "\n", encoding="utf-8")
            assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{context.artifacts_dir}"
            observed = json.loads((context.artifacts_dir / f"{phase}-result.json").read_text(encoding="utf-8"))
            assert observed["phase"] == "completed" and observed["pageErrors"] == []
            assert external.read_bytes() == shared_original, "表单编辑、复制及重启不能改动共享外部原件"
            if phase == "save":
                persisted = commentjson.loads(source.read_text(encoding="utf-8"))
                assert persisted == observed["savedDocument"], "真实来源文件必须与保存回执后重新 GET 的内容一致"
                fixture["copyId"] = observed["copyId"]
                await asyncio.to_thread(close_gateway_process, gateway)
                gateway = await asyncio.to_thread(start_gateway)
        assert catalog.model_requests == [], "节点表单与预检不能调用模型"
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
