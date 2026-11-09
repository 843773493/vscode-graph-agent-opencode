from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Generator
from contextlib import ExitStack
from pathlib import Path

import commentjson
import httpx
import pytest

from app.core.workspace_identity import read_workspace_id
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - pytest 显式导入共享构建装具
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
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)


@pytest.fixture(scope="module")
def chat_backend(
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
    with ExitStack() as services, model_stream_replay(
        context.artifacts_dir, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="real-web-composer-read-file-tool-loop",
    ) as replay:
        # 整个服务组保留绑定，避免等待闸门或启动其它服务时被客户端临时源端口抢占。
        lease = E2EProcessLease(
            context.runtime_root_for_node(request.node.nodeid) / "service-ports.json",
            tuple(port_block.port(offset) for offset in (0, 10, 20)),
        )
        lease.acquire()
        services.callback(lease.release)
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
    chat_backend: tuple[str, Path, ModelStreamReplay],
    web_workbench_assets: Path,
) -> None:
    project_root = Path.cwd().resolve()
    backend_url, workspace_root, replay = chat_backend
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("浏览器 Composer 集成需要 Chromium")

    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    gateway = start_gateway_process(
        workspace_root=workspace_root,
        default_backend_url=backend_url,
        port=port_block.port(20),
        extra_env={
            "BOXTEAM_WEB_ASSETS": str(web_workbench_assets)
        },
    )
    artifacts = context.artifacts_dir
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
            await acquire_gateway_guest(client)
            health = await client.get("/api/gateway/health")
            assert health.status_code == 200, health.text
            gateway_id = health.json()["data"]["gateway_id"]
            assert isinstance(gateway_id, str) and gateway_id
            workspace_id = read_workspace_id(workspace_root)
            workspaces = await client.get("/api/gateway/workspaces")
            assert workspaces.status_code == 200, workspaces.text
            active_workspace = {"gateway_id": gateway_id, "workspace_id": workspace_id}
            assert workspaces.json()["data"]["active_workspace"] == active_workspace
            assert health.json()["data"]["active_workspace"] == active_workspace
            assert len([
                item for item in workspaces.json()["data"]["items"]
                if (item["gateway_id"], item["workspace_id"]) == (gateway_id, workspace_id)
            ]) == 1

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
        parallel_interactions = [build_cassette(
            asset_id="browser-parallel-threads", model="browser-chat-tool-loop-stub",
            url=f"http://127.0.0.1:{port_block.port(10)}/v1/chat/completions",
            frames=chat_completion_frames(
                response_id=f"chatcmpl-parallel-{index}", model="browser-chat-tool-loop-stub",
                deltas=[{"role": "assistant", "content": text}], finish_reason="stop",
            ),
        ).raw["interactions"][0] for index, text in enumerate(
            ("主线程独立回复 alpha", "子线程独立回复 beta"), start=1,
        )]
        environment.update(
            {
                **replay.environment,
                "BOXTEAM_BROWSER_MODEL_INTERACTIONS": json.dumps(interactions),
                "BOXTEAM_BROWSER_PARALLEL_INTERACTIONS": json.dumps(parallel_interactions),
                "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
                "BOXTEAM_BROWSER_GATEWAY_ID": gateway_id,
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
        (artifacts / "browser.log").write_text(browser_result.stdout + browser_result.stderr, encoding="utf-8")
        (artifacts / "browser-process.json").write_text(json.dumps({
            "command": ["bun", "tests/integration/clients/web/basic_chat_tool_loop.mjs"],
            "exit_code": browser_result.returncode,
        }, indent=2) + "\n", encoding="utf-8")
        assert browser_result.returncode == 0, (
            "浏览器 Composer 工具循环失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["gatewayId"] == gateway_id
        assert result["workspaceId"] == workspace_id
        assert result["composerSendRequest"] is True
        assert result["sentMessage"] == "请读取 README.md，然后告诉我工具调用是否完成。"
        assert result["traceSseConnected"] is True
        assert result["messageSseConnected"] is True
        assert result["completedToolVisible"] is True
        assert "read_file" in result["completedToolText"]
        assert result["finalTextVisible"] is True
        assert result["noPageErrors"] is True, result["pageErrors"]
        assert result["workbenchLoaded"] is True
        clipboard_failure = result["clipboardFailure"]
        assert clipboard_failure["gatewayId"] == result["gatewayId"]
        assert clipboard_failure["workspaceId"] == result["workspaceId"]
        assert clipboard_failure["sessionId"] == result["sessionId"]
        assert clipboard_failure["counts"] == {"write": 1, "fallback": 1}
        assert clipboard_failure["errorVisible"] is True
        assert clipboard_failure["draftPreserved"] is True
        deletion = result["deletionRefresh"]
        assert deletion["sessionId"] == result["tabs"]["spareSessionId"]
        assert deletion["removedFromBothGroups"] is True
        assert deletion["survivorOperable"] is True
        assert deletion["receipt"]["state"] == "committed"
        assert deletion["receipt"]["settlement"] == "settled"
        assert deletion["restoredReads"] == []
        replay_failure = result["replayFailure"]
        assert replay_failure["threadId"] == result["threadId"]
        assert replay_failure["turnId"] == result["turnId"]
        assert replay_failure["requestId"]
        assert replay_failure["errorVisible"] is True
        assert replay_failure["editPreserved"] is True
        assert replay_failure["draftPreserved"] is True
        assert replay_failure["authoritativeHistoryReloaded"] is True
        assert replay_failure["chatOperable"] is True
        assert len(replay_failure["interceptions"]) == 1
        rejection = replay_failure["interceptions"][0]
        assert rejection["gatewayId"] == result["gatewayId"]
        assert rejection["workspaceId"] == result["workspaceId"]
        assert rejection["method"] == "POST"
        assert rejection["payload"]["action"] == "edit_and_continue"
        tabs = result["tabs"]
        assert tabs["previewReplaced"] is True
        assert tabs["treeDoubleClickPinned"] is True
        assert tabs["tabDoubleClickPinned"] is True
        assert tabs["closePreservedSessions"] is True
        assert tabs["lateNavigationPreservedActiveTab"] is True
        assert tabs["reopenedThreadId"] == result["threadId"]
        assert tabs["spareSessionId"] != result["sessionId"]
        assert tabs["spareThreadId"] != result["threadId"]

        call_ids = result["modelCallIds"]
        assert len(call_ids) == 2 and len(set(call_ids)) == 2
        recorded = [json.loads((replay.artifacts / f"{call_id}.json").read_text()) for call_id in call_ids]
        assert [record["identity"] for record in recorded] == [
            {"session_id": result["sessionId"], "thread_id": result["threadId"], "model_call_id": call_id}
            for call_id in call_ids
        ]
        with replay.control._condition:
            assert sum(count for namespace, count in replay.control._counters.items()
                       if namespace.startswith("model_call/")) == 4
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
        parallel = result["parallel"]
        assert parallel["sessionId"] == result["sessionId"]
        assert parallel["draftsIsolated"] is True
        assert parallel["concurrentCalls"] is True
        assert parallel["repliesIsolated"] is True
        assert parallel["groupResourcesIsolated"] is True
        interrupt_failure = parallel["interruptFailure"]
        assert interrupt_failure["errorVisible"] is True
        assert interrupt_failure["errorIsolated"] is True
        assert interrupt_failure["draftPreserved"] is True
        assert interrupt_failure["requestId"]
        assert interrupt_failure["sessionId"] == result["sessionId"]
        assert interrupt_failure["threadId"] == result["threadId"]
        assert interrupt_failure["activeJobId"] == parallel["receipts"][0]["job_id"]
        assert interrupt_failure["interceptions"] == [{
            "path": f"/api/v1/sessions/{result['sessionId']}/interrupt",
            "threadId": result["threadId"], "gatewayId": gateway_id, "workspaceId": workspace_id,
        }]
        calls = parallel["calls"]
        assert len(calls) == 2
        assert calls[0]["threadId"] == result["threadId"]
        assert calls[1]["threadId"] != result["threadId"]
        for index, call in enumerate(calls):
            record = json.loads((replay.artifacts / f"{call['callId']}.json").read_text())
            assert record["identity"] == {
                "session_id": result["sessionId"], "thread_id": call["threadId"], "model_call_id": call["callId"],
            }
            messages = record["request"]["messages"]
            user_inputs = [message["content"] for message in messages if message["role"] == "user"]
            assert call["message"] in user_inputs
            assert calls[1 - index]["message"] not in user_inputs
            assert messages[0] == first_messages[0]
            assert messages[1] == first_messages[1]
            # fork 复制 canonical 对话，CSM 在 child 首次执行时独立激活，位置不属于继承前缀。
            assert [message for message in messages if message["role"] in {"assistant", "tool"}] == [
                assistant, tool_result,
                {"role": "assistant", "content": [{"text": "浏览器 SSE 工具调用已完成。", "type": "text"}]},
            ]
            context_frames = [json.loads(message["content"]) for message in messages
                if message["role"] == "user" and isinstance(message["content"], str)
                and message["content"].startswith('{"protocol":"boxteam-context-sources:v2"')]
            assert len(context_frames) == 1
            assert context_frames[0]["sources"]
        file = result["file"]
        assert file["inlinePreviewFirst"] is True
        assert file["nativeEditorSaved"] is True
        assert file["path"].startswith("boxteam://")
        content = (workspace_root / "README.md").read_bytes()
        assert content.decode("utf-8") == file["content"]
        assert content.endswith("Workbench 原生编辑器保存验证\n".encode())
        assert hashlib.sha256(content).hexdigest() == file["revision"]
    finally:
        close_gateway_process(gateway)
