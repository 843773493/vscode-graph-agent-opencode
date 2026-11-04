"""真实 Web/Gateway/Backend 元数据初始化；共享回放拒绝非预期模型调用。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from pathlib import Path

import commentjson
import httpx
import pytest

from app.gateway.runtime.process_lifecycle import ManagedProcess
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式模块产物
)
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.scope import capture_test_processes, register_test_process
from tests.support.processes.workspace import (
    wait_for_http_ok,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(autouse=True)
def metadata_web_assets(monkeypatch: pytest.MonkeyPatch, web_workbench_assets: Path) -> None:
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))


@pytest.fixture
def metadata_port_reservations() -> Iterator[ExitStack]:
    with ExitStack() as reservations:
        yield reservations


@pytest.fixture
async def metadata_case(
    request: pytest.FixtureRequest, metadata_port_reservations: ExitStack,
) -> AsyncIterator[tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    lease, gateway_probes = E2EProcessLease.acquire_available(
        context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", range(ports.start, ports.end + 1),
    )
    metadata_port_reservations.callback(lease.release)
    gateway_port = lease.ports[0]
    provider_port = ports.port(10)
    (context.artifacts_dir / "port-allocation.json").write_text(
        json.dumps(
            {
                "test_file": ports.test_file.as_posix(),
                "block_index": ports.index,
                "block": {"start": ports.start, "end": ports.end},
                "gateway": {
                    "port": gateway_port,
                    "bind_probes": [
                        {"port": port, "available": available}
                        for port, available in gateway_probes
                    ],
                },
                "provider": {"port": provider_port, "transport": "model_stream_replay"},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root / context.run_root.name / context.runtime_root_for_node(request.node.nodeid).name / "metadata",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    (workspace / ".boxteam/workspace-identity.json").unlink(missing_ok=True)
    config_path = install_test_workspace_config(
        workspace_root=workspace, config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc",
    )
    config = commentjson.loads(config_path.read_text())
    for provider in config["llm"]["providers"]:
        provider.update({
            "endpoint": f"http://127.0.0.1:{provider_port}/v1", "model": "e2e-stub-model",
            "api_key": "e2e-local-model-key", "custom_llm_provider": "openai",
            "api_mode": {"protocol": "chat_completions", "model_info": {"supports_function_calling": True, "supports_reasoning": False},
                         "supports_reasoning": {"reasoning_content": False}},
        })
        provider.pop("auth", None)
        provider.pop("request_options", None)
    for agent in config["agents"].values():
        agent["model"]["fallback_providers"] = []
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    with model_stream_replay(
        context.output_root, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="metadata-and-unregistered-native-directory-without-model-call",
    ) as provider:
        gateway = await asyncio.to_thread(
            start_gateway_process, workspace_root=workspace, default_backend_url=None, port=gateway_port,
            boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
            extra_env=provider.environment,
        )
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway_port}", headers=LOCAL_TOKEN_HEADERS, timeout=90) as client:
                await acquire_gateway_guest(client)
                yield client, context, workspace, provider
        finally:
            await asyncio.to_thread(close_gateway_process, gateway)


async def metadata_data(client: httpx.AsyncClient, path: str):
    response = await client.get(path)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["X-Request-ID"] == payload["request_id"]
    return payload["data"]


@pytest.fixture(params=["static", "vite"])
async def metadata_frontend(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
    metadata_port_reservations: ExitStack,
) -> AsyncIterator[str]:
    client, context, _workspace, _provider = metadata_case
    if request.param == "static":
        yield str(client.base_url).rstrip("/")
        return
    runtime = context.runtime_root_for_node(request.node.nodeid)
    ports = integration_port_block_for_file(Path(request.node.path))
    lease, port_probes = E2EProcessLease.acquire_available(runtime / "vite-port.json", range(ports.start, ports.end + 1))
    metadata_port_reservations.callback(lease.release)
    port = lease.ports[0]
    allocation_path = context.artifacts_dir / "port-allocation.json"
    allocation = json.loads(allocation_path.read_text())
    allocation["frontend"] = {"port": port, "transport": "vite", "bind_probes": [
        {"port": candidate, "available": available} for candidate, available in port_probes
    ]}
    allocation_path.write_text(json.dumps(allocation, ensure_ascii=False, indent=2) + "\n")
    base_url = f"http://127.0.0.1:{port}"
    node = shutil.which("node")
    assert node is not None, "Workbench 源码开发入口需要 Node"
    # 与产品 dev 命令一致，直接持有实际监听的 Node，不把包管理器包装进程交给 PID 探针。
    command = [node, "scripts/dev.mjs", "--port", str(port), "--gateway", str(client.base_url).rstrip("/"),
               "--outdir", str(runtime / "vite-assets" / "dist"), "--cache", str(runtime / "vite-cache")]
    log_path = context.artifacts_dir / "vite.log"
    with log_path.open("x", encoding="utf-8") as log, capture_test_processes():
        process = await asyncio.to_thread(
            subprocess.Popen, command, cwd=Path.cwd() / "src/clients/workbench",
            stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        managed = ManagedProcess(process=process, log_file=log)
        register_test_process(managed, managed.close)
        await asyncio.to_thread(wait_for_http_ok, f"{base_url}/health", process, timeout_seconds=180)
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=90) as frontend:
            health = await frontend.get("/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok", "service": "frontend"}
            await acquire_gateway_guest(frontend)
            initialization = {path: await metadata_data(frontend, path) for path in (
                "/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace",
            )}
            html = await frontend.get("/")
            assert html.status_code == 200
            assert 'src="/@vite/client"' in html.text
            assert 'src="/src/main.ts"' in html.text
        (context.artifacts_dir / "vite-initialization.json").write_text(json.dumps({
            "command": command, "pid": process.pid, "base_url": base_url,
            "health": health.json(), "initialization": initialization,
        }, ensure_ascii=False, indent=2) + "\n")
        yield base_url


@pytest.mark.asyncio
async def test_workspace_metadata_without_root_initializes_real_web(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, ModelStreamReplay],
    metadata_frontend: str,
) -> None:
    client, context, workspace, provider = metadata_case
    await metadata_data(client, "/api/gateway/health")
    workspaces = await metadata_data(client, "/api/gateway/workspaces")
    active = next(item for item in workspaces["items"] if item["workspace_id"] == workspaces["active_workspace_id"])
    external = workspace.with_name("未注册目录 Ω")
    external.mkdir()
    external_file = external / "未注册 %.txt"
    external_file.write_text("未注册目录原正文\n", encoding="utf-8")
    created = await client.post("/api/v1/sessions", json={"title": "原生目录浏览编辑"})
    assert created.status_code == 200, created.text
    session_id = created.json()["data"]["session_id"]
    thread_id = created.json()["data"]["thread_id"]
    assert isinstance(thread_id, str) and thread_id
    shortcut = await client.post(f"/api/v1/sessions/{session_id}/file-tree-shortcuts",
                                 json={"path": str(external), "label": "未注册目录"})
    assert shortcut.status_code == 200, shortcut.text
    root_uri = shortcut.json()["data"]["effective_shortcuts"][0]["path"]
    assert root_uri.startswith("boxteam://gateway/")
    assert str(external) not in shortcut.text
    responses = {}
    for endpoint in ("/api/v1/workspace", "/api/v1/workspace/context"):
        data = await metadata_data(client, endpoint)
        assert data["workspace_id"] == active["backend_workspace_id"]
        assert "root_path" not in data and "root" not in data["git"]
        assert str(workspace) not in json.dumps(data, ensure_ascii=False)
        responses[endpoint] = data
    (context.artifacts_dir / "metadata-http.json").write_text(json.dumps(responses, ensure_ascii=False, indent=2) + "\n")
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    assert chromium is not None, "元数据 Web Integration 需要 Chromium"
    result_path = context.artifacts_dir / "workspace-metadata-browser.json"
    environment = {
        **os.environ,
        "BOXTEAM_BROWSER_BASE_URL": metadata_frontend,
        "BOXTEAM_BROWSER_FIXTURE": json.dumps({"workspaceId": active["workspace_id"], "backendWorkspaceId": active["backend_workspace_id"],
            "nativeDirectory": {"sessionId": session_id, "threadId": thread_id, "rootUri": root_uri, "fileName": external_file.name,
                                "content": "未注册目录原正文\n", "edited": "浏览器修改后的未注册目录正文 Ω\n"}}),
        "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
        "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "workspace-metadata-browser.png"),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    temporary = context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp"
    with chromium_temporary_environment(temporary) as temporary_environment:
        environment.update(temporary_environment)
        process = await asyncio.to_thread(
            subprocess.run, ["bun", "tests/integration/clients/web/workspace_metadata_bootstrap.mjs"],
            cwd=Path.cwd(), env=environment, capture_output=True, text=True, timeout=90, check=False,
        )
    (context.artifacts_dir / "playwright.log").write_text(process.stdout + process.stderr)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(result_path.read_text())
    assert result["metadata"]["data"]["workspace_id"] == active["backend_workspace_id"]
    assert result["page_errors"] == []
    assert result["route_stubs"] == []
    assert result["workspace_navigation_visible"] is True
    assert result["native_directory_reopened"] is True
    theme_warning = result["theme_background_warning"]
    assert theme_warning["http_status"] == 404
    assert theme_warning["request_id"]
    assert theme_warning["asset_id"] == "0" * 64
    assert theme_warning["url"].endswith("/api/gateway/ui-assets/" + "0" * 64)
    assert theme_warning["notification"] == (
        "主题背景图加载失败，已回退为无背景图：背景图片加载失败: " + theme_warning["url"]
    )
    assert theme_warning["hosts"] >= 2
    assert result["settings_reload_rounds"] == [
        {"fontSize": size, "phase": "restored", "restoredFontSize": f"{size}px"}
        for size in (16, 19, 17)
    ]
    assert result["controlled_shutdown"]["after"] > result["controlled_shutdown"]["before"]
    assert result["controlled_shutdown"]["keys"]
    assert external_file.read_text(encoding="utf-8") == "浏览器修改后的未注册目录正文 Ω\n"
    assert not (external / ".boxteam").exists()
    after = await metadata_data(client, "/api/gateway/workspaces")
    assert [(item["workspace_id"], item["backend_workspace_id"]) for item in after["items"]] == [
        (item["workspace_id"], item["backend_workspace_id"]) for item in workspaces["items"]]
    # 在 transport 匹配前核真实 ModelCall 分配，失败的意外调用也不能伪装成零请求。
    with provider.control._condition:
        calls = {namespace: count for namespace, count in provider.control._counters.items()
                 if namespace.startswith("model_call/")}
    (context.artifacts_dir / "provider-call-counts.json").write_text(json.dumps(calls) + "\n")
    assert calls == {}, "元数据初始化与原生目录编辑不应发送模型请求"

