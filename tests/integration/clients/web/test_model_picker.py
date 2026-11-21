"""真实 Workbench 模型选择、偏好隔离及显式发现／推理的暖请求预算。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import commentjson
import httpx
import pytest

from app.core.workspace_identity import read_workspace_id
from configs.installer import install_user_configuration
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.backend.config.test_llm_discovery import Catalog
from tests.integration.backend.config.test_llm_discovery import (
    catalog as catalog,  # noqa: PLC0414
)
from tests.integration.clients.web.test_model_connection_probe import (
    probe_service_ports as probe_service_ports,  # noqa: PLC0414
)
from tests.integration.clients.web.test_model_connection_probe import (
    probe_web_assets as probe_web_assets,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


async def test_model_picker_uses_native_controls_and_backend_identity(
    request: pytest.FixtureRequest, catalog: Catalog, probe_web_assets: Path,
    probe_service_ports: tuple[int, int], integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    source = Path(integration_workspace_config_path)
    backend_port, gateway_port = probe_service_ports
    config = commentjson.loads(source.read_text(encoding="utf-8"))
    provider = config["llm"]["providers"]["primary"]
    provider["name"] = "团队模型服务"
    secret_id = f"sec_{uuid4().hex}"
    provider["defaults"].update({"endpoint": catalog.endpoint, "api_key": f"secret:{secret_id}"})
    providers = {
        **commentjson.loads(Path("configs/workspace_inline.jsonc").read_text(encoding="utf-8"))["llm"]["providers"],
        **config["llm"]["providers"],
    }
    for index in range(20 - len(providers)):
        providers[f"catalog-{index}"] = deepcopy(provider)
    model_ids = ["default", "backup", "catalog-only", *(f"model-{index}" for index in range(7))]
    for definition in providers.values():
        definition["models"] = {key: {"model": f"picker-{key}"} for key in model_ids}
    provider["models"]["default"]["name"] = "日常助手"
    provider["models"]["backup"]["name"] = "备用助手"
    home = context.boxteam_home_for_node(request.node.nodeid)
    install_user_configuration(config_root=home / "config", profile="default", project_root=Path.cwd())
    user_source = home / "config/workspace.jsonc"
    user_source.write_text(json.dumps({"config_version": 3, "llm": {"providers": providers}}, ensure_ascii=False) + "\n", encoding="utf-8")
    original_user_source = user_source.read_bytes()
    # 内置定义、用户目录和工作区局部覆盖走真实三层加载，不预写执行或展示状态。
    config["llm"] = {"providers": {"primary": {"models": {"backup": {
        "overrides": {"request_options": {"parameters": {"temperature": 0}}},
    }}}}}
    for agent_id, agent in config["agents"].items():
        agent["model"]["primary"] = {"provider_id": "primary", "model_id": "default"}
        agent["model"]["fallbacks"] = ([{"provider_id": "primary", "model_id": "backup"}]
                                         if agent_id == "default" else [])
    config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = f"http://127.0.0.1:{gateway_port}"
    source.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    original_source = source.read_bytes()
    backend = await asyncio.to_thread(start_backend_process, workspace_root=str(workspace), port=backend_port,
        boxteam_home=home, artifacts_dir=context.artifacts_dir / "backend", log_name="model-picker-backend",
        refresh_user_configuration=False)
    try:
        gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspace,
            default_backend_url=f"http://127.0.0.1:{backend.port}", port=gateway_port, boxteam_home=home,
            artifacts_dir=context.artifacts_dir / "gateway", extra_env={"BOXTEAM_WEB_ASSETS": str(probe_web_assets)})
        try:
            first_reads = []
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS) as client:
                created = await client.post("/api/gateway/secrets", json={"id": secret_id, "name": "模型目录共享凭据", "value": "fixture-model-key"})
                created.raise_for_status()
                gateway_id = created.json()["data"]["gateway_id"]
                owner_headers = {"X-BoxTeam-Target-Gateway-Id": gateway_id, "X-BoxTeam-Workspace-Id": read_workspace_id(workspace)}
                for path in ("/api/v1/agents", "/api/v1/config/sources", "/api/v1/config/sources/workspace_root_mutable_override"):
                    started = time.perf_counter()
                    response = await client.get(path, headers=owner_headers)
                    response.raise_for_status()
                    envelope = response.json()
                    assert response.headers["X-Request-ID"] == envelope["request_id"]
                    first_reads.append({"path": path, "elapsedMs": (time.perf_counter() - started) * 1000,
                                        "bytes": len(response.content), "requestId": envelope["request_id"]})
            (context.artifacts_dir / "first-model-reads.json").write_text(json.dumps(first_reads, indent=2) + "\n", encoding="utf-8")
            executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
            assert executable, "模型选择验收需要 Chromium"
            command = ["timeout", "180", "bash", "-c", 'ulimit -d 4194304; exec "$@"', "bash",
                       "bun", "tests/integration/clients/web/model_picker.mjs"]
            with chromium_temporary_environment(context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
                result = await asyncio.to_thread(subprocess.run, command, cwd=Path.cwd(), capture_output=True,
                    text=True, check=False, timeout=190, env=os.environ | temporary | {
                        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                        "BOXTEAM_MODEL_PICKER_FIXTURE": json.dumps({"baseUrl": f"http://127.0.0.1:{gateway.port}",
                            "artifacts": str(context.artifacts_dir), "firstReads": first_reads}),
                    })
            (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
            (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
            (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}) + "\n")
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}\n{context.artifacts_dir}"
            observed = json.loads((context.artifacts_dir / "result.json").read_text(encoding="utf-8"))
            assert observed["phase"] == "complete" and observed["pageErrors"] == []
            assert len(observed["sessions"]) == 6
            assert len(catalog.requests) == 42 and len(catalog.model_requests) == 21
            assert [path for path, _ in catalog.requests] == ["/v1/models", "/v1/chat/completions"] * 21
            assert all(body["model"] == "picker-backup" and body["stream"] is True for body in catalog.model_requests)
            assert all(headers["Authorization"] == "Bearer fixture-model-key" for _, headers in catalog.requests)
            assert source.read_bytes() == original_source, "展示偏好和会话选择不能修改模型配置来源"
            assert user_source.read_bytes() == original_user_source
            assert all(sample["elapsedMs"] <= 3000 for sample in first_reads), first_reads
        finally:
            await asyncio.to_thread(close_gateway_process, gateway)
    finally:
        await asyncio.to_thread(close_backend_process, backend)
