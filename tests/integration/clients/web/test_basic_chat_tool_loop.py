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

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.fixture(scope="module")
def browser_backend(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> Generator[tuple[str, Path, ModelStreamReplay], None, None]:
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
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with model_stream_replay(
        context.output_root, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="real-web-composer-read-file-tool-loop",
    ) as replay:
        backend = start_backend_process(
            workspace_root=str(workspace_root),
            port=port_block.port(0),
            log_name="basic-chat-tool-loop-browser-backend",
            asgi_app="tests.support.e2e_control.backend_app:app",
            env_overrides={
                "BOXTEAM_TEST_MODEL_API_KEY": "e2e-local-model-key",
                "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{port_block.port(20)}",
                **replay.environment,
            },
        )
        try:
            yield f"http://127.0.0.1:{backend.port}", workspace_root, replay
        finally:
            close_backend_process(backend)


@pytest.mark.asyncio
async def test_basic_chat_tool_loop_through_real_browser_composer(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path, ModelStreamReplay],
) -> None:
    project_root = Path.cwd().resolve()
    backend_url, workspace_root, replay = browser_backend
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
        responses = [
            ([{"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_browser_chat_tool_loop", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"'},
            }]}, {"tool_calls": [{"index": 0, "function": {"arguments": 'README.md"}'}}]}], "tool_calls"),
            ([{"role": "assistant", "content": "浏览器 SSE 工具调用已完成。"}], "stop"),
        ]
        interactions = [build_cassette(
            asset_id="browser-chat-tool-loop", model="browser-chat-tool-loop-stub",
            url=f"http://127.0.0.1:{port_block.port(10)}/v1/chat/completions",
            frames=chat_completion_frames(
                response_id=f"chatcmpl-browser-tool-loop-{index}", model="browser-chat-tool-loop-stub",
                deltas=deltas, finish_reason=finish_reason,
            ),
        ).raw["interactions"][0] for index, (deltas, finish_reason) in enumerate(responses, start=1)]
        environment.update(
            {
                **replay.environment,
                "BOXTEAM_BROWSER_MODEL_INTERACTIONS": json.dumps(interactions),
                "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
        temporary = context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp"
        with chromium_temporary_environment(temporary) as temporary_environment:
            environment.update(temporary_environment)
            browser_result = await asyncio.to_thread(
                subprocess.run,
                ["bun", "tests/integration/clients/web/basic_chat_tool_loop.mjs"],
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

        call_ids = result["modelCallIds"]
        assert len(call_ids) == 2 and len(set(call_ids)) == 2
        recorded = [json.loads((replay.artifacts / f"{call_id}.json").read_text()) for call_id in call_ids]
        assert [record["identity"] for record in recorded] == [
            {"session_id": result["sessionId"], "thread_id": result["threadId"], "model_call_id": call_id}
            for call_id in call_ids
        ]
        with replay.control._condition:
            assert sum(count for namespace, count in replay.control._counters.items()
                       if namespace.startswith("model_call/")) == 2
        first_request, second_request = [record["request"] for record in recorded]
        first_messages = first_request["messages"]
        assert first_messages[0]["role"] == "system"
        assert first_messages[1] == {"role": "user", "content": result["sentMessage"]}
        # CSM 来源按合同使用独立 user 帧；工具续轮须逐字保留已封存前缀。
        assert all(message["role"] == "user" for message in first_messages[1:])
        assert second_request["messages"][:-2] == first_messages
        assistant, tool_result = second_request["messages"][-2:]
        assert [assistant["role"], tool_result["role"]] == ["assistant", "tool"]
        assert len(assistant["tool_calls"]) == 1
        tool_call = assistant["tool_calls"][0]
        assert tool_call["function"]["name"] == "read_file"
        assert json.loads(tool_call["function"]["arguments"]) == {"path": "README.md"}
        assert tool_result["tool_call_id"] == tool_call["id"]
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
