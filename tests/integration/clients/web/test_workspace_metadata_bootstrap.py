"""真实 Web/Gateway/Backend 元数据初始化；Provider 使用本地 HTTP 协议桩。"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import socket
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.stubs.http_stubs import HTTPStubState, openai_chat_stub
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import TestFilePortBlock, integration_port_block_for_file
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


def _select_free_port(
    port_block: TestFilePortBlock,
    *,
    excluded: frozenset[int] = frozenset(),
) -> tuple[int, list[tuple[int, bool]]]:
    probes: list[tuple[int, bool]] = []
    for port in range(port_block.start, port_block.end + 1):
        if port in excluded:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind(("127.0.0.1", port))
            except OSError as error:
                if error.errno != errno.EADDRINUSE and getattr(error, "winerror", None) != 10048:
                    raise RuntimeError(f"测试端口预探测失败: port={port}") from error
                probes.append((port, False))
                continue
        probes.append((port, True))
        return port, probes
    raise RuntimeError(
        f"测试文件端口块没有空闲端口: file={port_block.test_file}, "
        f"start={port_block.start}, end={port_block.end}"
    )


@contextmanager
def _chromium_temporary_environment(
    context: TestRunContext,
    node_id: str,
) -> Iterator[dict[str, str]]:
    temporary = context.runtime_root_for_node(node_id) / "chromium-tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    if Path("/proc/self/fd").is_dir():
        descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        short_path = f"/proc/{os.getpid()}/fd/{descriptor}"
    else:
        short_path = str(temporary)
    try:
        if Path(short_path).resolve() != temporary.resolve():
            raise RuntimeError(f"Chromium 临时目录别名不匹配: {short_path}")
        yield {name: short_path for name in ("TMPDIR", "TMP", "TEMP")}
    finally:
        if descriptor is not None:
            os.close(descriptor)


@pytest.fixture(autouse=True)
def metadata_web_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    assets = Path.cwd() / "src/clients/web/dist"
    assert (assets / "index.html").is_file(), "先对本批源码执行 Web build，再复用其产物验证初始化"
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


@pytest.fixture
async def metadata_case(request: pytest.FixtureRequest) -> AsyncIterator[tuple[httpx.AsyncClient, TestRunContext, Path, HTTPStubState]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    gateway_port, gateway_probes = _select_free_port(ports)
    provider_port, provider_probes = _select_free_port(
        ports,
        excluded=frozenset({gateway_port}),
    )
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
                "provider_stub": {
                    "port": provider_port,
                    "bind_probes": [
                        {"port": port, "available": available}
                        for port, available in provider_probes
                    ],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root / "metadata",
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
    with openai_chat_stub(provider_port) as provider:
        gateway = await asyncio.to_thread(
            start_gateway_process, workspace_root=workspace, default_backend_url=None, port=gateway_port,
            boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
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


@pytest.mark.asyncio
async def test_workspace_metadata_without_root_initializes_real_web(
    request: pytest.FixtureRequest,
    metadata_case: tuple[httpx.AsyncClient, TestRunContext, Path, HTTPStubState],
) -> None:
    client, context, workspace, provider = metadata_case
    await metadata_data(client, "/api/gateway/health")
    workspaces = await metadata_data(client, "/api/gateway/workspaces")
    active = next(item for item in workspaces["items"] if item["workspace_id"] == workspaces["active_workspace_id"])
    external = context.workspace_root / "未注册目录 Ω"
    external.mkdir()
    external_file = external / "未注册 %.txt"
    external_file.write_text("未注册目录原正文\n", encoding="utf-8")
    created = await client.post("/api/v1/sessions", json={"title": "原生目录浏览编辑"})
    assert created.status_code == 200, created.text
    session_id = created.json()["data"]["session_id"]
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
        "BOXTEAM_BROWSER_BASE_URL": str(client.base_url).rstrip("/"),
        "BOXTEAM_BROWSER_FIXTURE": json.dumps({"workspaceId": active["workspace_id"], "backendWorkspaceId": active["backend_workspace_id"],
            "nativeDirectory": {"sessionId": session_id, "rootUri": root_uri, "fileName": external_file.name,
                                "content": "未注册目录原正文\n", "edited": "浏览器修改后的未注册目录正文 Ω\n"}}),
        "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
        "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "workspace-metadata-browser.png"),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    with _chromium_temporary_environment(context, request.node.nodeid) as temporary_environment:
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
    assert external_file.read_text(encoding="utf-8") == "浏览器修改后的未注册目录正文 Ω\n"
    assert not (external / ".boxteam").exists()
    after = await metadata_data(client, "/api/gateway/workspaces")
    assert [(item["workspace_id"], item["backend_workspace_id"]) for item in after["items"]] == [
        (item["workspace_id"], item["backend_workspace_id"]) for item in workspaces["items"]]
    assert provider.requests == [], "元数据初始化不应发送模型请求"

