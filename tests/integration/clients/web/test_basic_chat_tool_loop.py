from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.integration.stubs.http_stubs import (
    HTTPStubState,
    openai_chat_tool_loop_stub,
)
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import close_backend_process, start_backend_process


@pytest.fixture(scope="module")
def browser_backend(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> Generator[tuple[str, Path, HTTPStubState], None, None]:
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    workspace_root = Path(integration_workspace_root_path).resolve()
    config_path = Path(integration_workspace_config_path)
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    provider = next(
        item for item in config["llm"]["providers"] if item["id"] == "primary"
    )
    provider.update(
        {
            "endpoint": f"http://127.0.0.1:{port_block.port(10)}/v1",
            "model": "browser-chat-tool-loop-stub",
            "api_key": "${BOXTEAM_TEST_MODEL_API_KEY}",
            "custom_llm_provider": "openai",
        }
    )
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with openai_chat_tool_loop_stub(port_block.port(10)) as model_stub:
        backend = start_backend_process(
            workspace_root=str(workspace_root),
            port=port_block.port(0),
            log_name="basic-chat-tool-loop-browser-backend",
            env_overrides={"BOXTEAM_TEST_MODEL_API_KEY": "e2e-local-model-key"},
        )
        try:
            yield f"http://127.0.0.1:{backend.port}", workspace_root, model_stub
        finally:
            close_backend_process(backend)


@pytest.mark.asyncio
async def test_basic_chat_tool_loop_through_real_browser_composer(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path, HTTPStubState],
) -> None:
    project_root = Path.cwd().resolve()
    backend_url, workspace_root, model_stub = browser_backend
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("浏览器 Composer 集成需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    port_block = integration_port_block_for_file(Path(request.node.fspath))
    gateway = start_gateway_process(
        workspace_root=workspace_root,
        default_backend_url=backend_url,
        port=port_block.port(20),
        extra_env={
            "BOXTEAM_WEB_ASSETS": str(project_root / "src" / "clients" / "web" / "dist")
        },
    )
    artifacts = workspace_root.parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "basic-chat-tool-loop-result.json"
    screenshot_path = artifacts / "basic-chat-tool-loop-failure.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            guest = await client.post(
                "/api/gateway/users/guest",
                json={"tracking": {"source": "basic-chat-tool-loop-test"}},
            )
            assert guest.status_code == 200, guest.text
            workspaces = await client.get("/api/gateway/workspaces")
            assert workspaces.status_code == 200, workspaces.text
            workspace_id = workspaces.json()["data"]["active_workspace_id"]
            assert isinstance(workspace_id, str) and workspace_id

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        browser_result = await asyncio.to_thread(
            subprocess.run,
            [
                "node",
                "tests/integration/clients/web/basic_chat_tool_loop.mjs",
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        assert browser_result.returncode == 0, (
            "浏览器 Composer 工具循环失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["composerSendRequest"] is True
        assert result["sentMessage"] == "请读取 README.md，然后告诉我工具调用是否完成。"
        assert result["traceSseConnected"] is True
        assert result["messageSseConnected"] is True
        assert result["completedToolVisible"] is True
        assert "read_file" in result["completedToolText"]
        assert result["finalTextVisible"] is True
        assert result["noPageErrors"] is True, result["pageErrors"]

        assert len(model_stub.requests) == 2
        first_request = model_stub.requests[0]["json"]
        second_request = model_stub.requests[1]["json"]
        assert [message["role"] for message in first_request["messages"]] == [
            "system",
            "user",
        ]
        assert [message["role"] for message in second_request["messages"]] == [
            "system",
            "user",
            "assistant",
            "tool",
        ]
        assert first_request["stream"] is True
        assert second_request["stream"] is True
        assert any(
            tool["function"]["name"] == "read_file"
            for tool in first_request["tools"]
        )
        tool_messages = [
            message
            for message in second_request["messages"]
            if message["role"] == "tool"
        ]
        assert len(tool_messages) == 1
        assert "# 统一测试工作区" in tool_messages[0]["content"]
    finally:
        close_gateway_process(gateway)
