"""真实 Gateway／Workbench 的目录导入及 HTTP／SDK 流式、非流式验证。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import commentjson
import httpx
import pytest

from app.core.workspace_identity import read_workspace_id
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
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.fixture
def probe_web_assets(request: pytest.FixtureRequest) -> Path:
    prepared = os.getenv("BOXTEAM_SOURCE_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file()
    return assets


@pytest.fixture
def probe_service_ports(request: pytest.FixtureRequest) -> Iterator[tuple[int, int]]:
    ports = integration_ports_for_file(Path(request.node.path))
    yield ports.port(0), ports.port(20)


async def test_models_are_discovered_and_tested_in_original_settings(
    request: pytest.FixtureRequest, catalog: Catalog, probe_web_assets: Path, probe_service_ports: tuple[int, int],
    integration_workspace_root_path: str, integration_workspace_config_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    backend_port, gateway_port = probe_service_ports
    workspace = Path(integration_workspace_root_path)
    source = Path(integration_workspace_config_path)
    config = commentjson.loads(source.read_text(encoding="utf-8"))
    primary = config["profiles"]["provider.primary"]["settings"]["model_options"]
    primary.update({"endpoint": catalog.endpoint, "api_key": "${BOXTEAM_TEST_MODEL_API_KEY}",
        "request_options": {}})
    config["profiles"]["provider.primary"]["settings"]["network"] = {"proxy": {"mode": "direct"}}
    business = config["settings"]["workspace"]
    business["llm"]["providers"]["primary"]["models"]["default"]["model"] = "upstream-main"
    business.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = f"http://127.0.0.1:{gateway_port}"
    source.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    original_source = source.read_bytes()
    catalog.pages = [{"data": [{"id": "upstream-main"}, {"id": "upstream-new", "display_name": "目录新模型"}]}]
    catalog.directory_pages = {
        "/late/v1/models": {"data": [{"id": "stale-only"}]},
        "/current/v1/models": {"data": [{"id": "current-only"}]},
        "/partial/v1/models": {"data": [{"id": f"partial-{index}"} for index in range(5001)]},
    }
    catalog.text = "PROBE_VISIBLE_BEFORE_TERMINAL"
    catalog.pause_before_terminal = True
    catalog.blocked_models = {f"blocked-{index}" for index in range(1, 5)}
    catalog.delayed_models = {"slow-content": 16}
    catalog.model_events_path = context.artifacts_dir / "upstream-connections.jsonl"
    catalog.model_events_path.write_text("", encoding="utf-8")
    home = context.boxteam_home_for_node(request.node.nodeid)
    backend = await asyncio.to_thread(start_backend_process, workspace_root=str(workspace), port=backend_port,
        log_name="model-probe-backend", boxteam_home=home, artifacts_dir=context.artifacts_dir / "backend",
        env_overrides={"BOXTEAM_TEST_MODEL_API_KEY": "fixture-model-key", "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{gateway_port}",
                       "BOXTEAM_RUNTIME_CONTROL_TOKEN": LOCAL_TOKEN_HEADERS["X-Local-Token"]})
    try:
        gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspace,
            default_backend_url=f"http://127.0.0.1:{backend.port}", port=gateway_port, boxteam_home=home,
            artifacts_dir=context.artifacts_dir / "gateway", extra_env={"BOXTEAM_WEB_ASSETS": str(probe_web_assets)})
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS) as client:
                gateway_id = (await client.get("/api/gateway/health")).raise_for_status().json()["data"]["gateway_id"]
            executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
            assert executable, "模型连接验收需要 Chromium"
            visible = context.artifacts_dir / "content-visible.json"
            visible.unlink(missing_ok=True)
            command = ["timeout", "150s", "bash", "-c", 'ulimit -d 4194304; exec "$@"', "bash",
                       "bun", "tests/integration/clients/web/model_connection_probe.mjs"]
            with chromium_temporary_environment(context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
                running = asyncio.create_task(asyncio.to_thread(subprocess.run, command, cwd=Path.cwd(), capture_output=True,
                    text=True, check=False, timeout=160, env=os.environ | temporary | {
                        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                        "BOXTEAM_MODEL_PROBE_FIXTURE": json.dumps({"baseUrl": f"http://127.0.0.1:{gateway.port}",
                            "gatewayId": gateway_id, "workspaceId": read_workspace_id(workspace), "endpoint": catalog.endpoint,
                            "artifacts": str(context.artifacts_dir), "visibleMarker": str(visible),
                            "modelEvents": str(catalog.model_events_path)}),
                    }))
                try:
                    async with asyncio.timeout(155):
                        while not visible.exists() and not running.done():
                            await asyncio.sleep(0.02)
                        if visible.exists():
                            assert catalog.content_sent.is_set()
                        catalog.release_terminal.set()
                        result = await running
                finally:
                    catalog.release_terminal.set()
                    if not running.done():
                        await running
            (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
            (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
            (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}) + "\n")
            assert result.returncode == 0, f"{result.stdout}\n{result.stderr}\n{context.artifacts_dir}"
            observed = json.loads((context.artifacts_dir / "result.json").read_text(encoding="utf-8"))
            (context.artifacts_dir / "upstream-requests.json").write_text(
                json.dumps(catalog.model_requests, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            assert observed["phase"] == "complete" and not observed["pageErrors"]
            assert visible.exists(), "真实终态放行前必须先观察到页面正文"
            assert len(catalog.requests) == 26 and len(catalog.model_requests) == 18
            requests, model_requests = catalog.requests[:22], catalog.model_requests[:14]
            streaming, nonstreaming = model_requests[:2]
            assert streaming["stream"] is True
            assert streaming["temperature"] == 0
            assert "top_p" not in streaming and streaming["extension_switch"] is False and streaming["extension_value"] is None
            # Chat adapter 沿既有 SDK 合同省略 false；省略与显式 false 都表示非流式。
            assert nonstreaming.get("stream", False) is False
            assert "stream_options" not in nonstreaming
            assert "temperature" not in nonstreaming and "top_p" not in nonstreaming
            assert nonstreaming["extension_switch"] is False and nonstreaming["extension_value"] is None
            assert not any(key in body for body in (streaming, nonstreaming) for key in ("max_tokens", "max_output_tokens"))
            assert sorted(body.get("stream", False) for body in model_requests[2:4]) == [False, True]
            assert all(body["model"] == "upstream-main" for body in model_requests[:5])
            assert [body.get("stream", False) for body in model_requests[5:7]] == [True, False]
            assert {body["model"] for body in model_requests[7:10]} == {"blocked-1", "blocked-2", "blocked-3"}
            assert model_requests[-4]["model"] == "slow-content"
            assert [body["model"] for body in model_requests[-3:]] == ["upstream-main"] * 3
            assert all(headers["X-Active-Source"] == "browser-active" for _, headers in requests[-5:-2])
            assert all("X-Active-Source" not in headers for _, headers in requests[-2:])
            assert "X-Pending-Source" not in requests[-3][1], "active 不得使用 pending 来源"
            assert requests[-1][1]["X-Pending-Source"] == "browser-pending"
            assert [path for path, _ in catalog.requests[22:]] == [
                "/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/chat/completions",
            ]
            for body in catalog.model_requests[14:17]:
                assert body["model"] == "upstream-main" and body.get("stream", False) is False
                assert body["temperature"] == 0 and "top_p" not in body
                extensions = body.get("extra_body", body)
                assert extensions["extension_switch"] is False and extensions["extension_value"] is None
            assert len(observed["protocolWires"]) == 3 and observed["eofBoundary"]
            assert source.read_bytes() == original_source, "发现、导入草稿及测试都不能自行保存来源"
        finally:
            await asyncio.to_thread(close_gateway_process, gateway)
    finally:
        await asyncio.to_thread(close_backend_process, backend)
