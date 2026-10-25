from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Generator
from pathlib import Path
from uuid import uuid4

import commentjson
import httpx
import pytest

from app.core.workspace_identity import read_workspace_id
from app.domain.itemized.enums import CanonicalItemStatus, PayloadKind, SemanticKind
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
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
from tests.support.rollout.web_boundary_seeding import (
    boundary_item,
    seed_boundary_turn,
    tool_call_payload,
    tool_result_payload,
)
from tests.support.session_catalog_owner import fixture_catalog_scope


def _seed_rollout(workspace_root: Path, session_id: str, count: int) -> None:
    """只准备用户视图恢复需要的展示历史；后台通知仍通过真实消息入口执行。"""
    sessions_dir = workspace_root / ".boxteam/sessions"
    with fixture_catalog_scope():
        for turn_index in range(1, count + 1):
            turn_id = f"job-{turn_index:04d}"
            call_id = f"call-{turn_index:04d}"
            model_call_id = f"model-call-{turn_id}"
            name = "read_fixture"
            call = tool_call_payload(call_id, name, {"path": f"fixture/{turn_index:04d}.json"}, model_call_id=model_call_id)
            result = tool_result_payload(call_id, name, json.dumps({"turn": turn_index, "result": "fixture result"}), model_call_id=model_call_id)
            final_id = f"item-assistant-final-{turn_index:04d}"
            items = (
                boundary_item(item_id=f"item-assistant-tool-{turn_index:04d}", semantic_kind=SemanticKind.TOOL_CALL,
                    payload_kind=PayloadKind.TOOL_CALL, status=CanonicalItemStatus.COMPLETED,
                    payload=call, turn_id=turn_id, projection_identity=ItemProjectionIdentity(
                        model_call_id=model_call_id, block_id=call["tool_call_id"], block_index=0)),
                boundary_item(item_id=f"item-tool-result-{turn_index:04d}", semantic_kind=SemanticKind.TOOL_RESULT,
                    payload_kind=PayloadKind.TOOL_RESULT, status=CanonicalItemStatus.COMPLETED,
                    payload=result, turn_id=turn_id, wire_role="tool"),
                boundary_item(item_id=final_id, semantic_kind=SemanticKind.ASSISTANT_OUTPUT,
                    payload_kind=PayloadKind.TEXT, status=CanonicalItemStatus.COMPLETED,
                    payload=f"模型最终响应 {turn_index}", turn_id=turn_id),
            )
            seed_boundary_turn(sessions_dir, session_id=session_id, turn_id=turn_id,
                user_content=f"用户问题 {turn_index}", items=items, outcome="completed",
                turn_status="completed", final_item_id=final_id)


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
    for provider in config["llm"]["providers"]:
        provider.update({
            "endpoint": f"http://127.0.0.1:{port_block.port(10)}/v1",
            "model": "e2e-stub-model", "api_key": "e2e-local-model-key",
            "custom_llm_provider": "openai",
            "api_mode": {"protocol": "chat_completions", "model_info": {
                "supports_function_calling": True, "supports_reasoning": True,
            }, "supports_reasoning": {"reasoning_content": True}},
        })
        provider.pop("auth", None)
        provider.pop("request_options", None)
    for agent in config["agents"].values():
        agent["model"]["fallback_providers"] = []
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with model_stream_replay(
        context.output_root, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="gateway-user-view-and-real-background-result-notification",
    ) as replay:
        backend = start_backend_process(
            workspace_root=str(workspace_root),
            port=port_block.port(0),
            log_name="gateway-user-view-backend",
            asgi_app="tests.support.e2e_control.backend_app:app",
            env_overrides={**replay.environment, "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{port_block.port(20)}"},
        )
        try:
            yield f"http://127.0.0.1:{backend.port}", workspace_root, replay
        finally:
            close_backend_process(backend)


@pytest.fixture
async def browser_backend_client(
    browser_backend: tuple[str, Path, ModelStreamReplay],
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=browser_backend[0],
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": read_workspace_id(browser_backend[1])},
        timeout=60,
    ) as client:
        yield client


async def _create_session(client: httpx.AsyncClient, title: str) -> str:
    response = await client.post("/api/v1/sessions", json={"title": title})
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["session_id"])


@pytest.mark.asyncio
async def test_gateway_user_view_sessions_two_browser_chain(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path, ModelStreamReplay],
    browser_backend_client: httpx.AsyncClient,
) -> None:
    project_root = Path.cwd().resolve()
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("Gateway 用户视图浏览器集成需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    session_id = await _create_session(browser_backend_client, "双浏览器视图会话")
    unopened_session_id = await _create_session(
        browser_backend_client,
        "未打开会话通知",
    )
    _seed_rollout(Path(browser_backend[1]), session_id, count=8)
    replay = browser_backend[2]
    main_thread = await browser_backend_client.get(f"/api/v1/sessions/{unopened_session_id}/main-thread")
    assert main_thread.status_code == 200, main_thread.text
    unopened_thread_id = main_thread.json()["data"]["thread_id"]
    history = await browser_backend_client.post(f"/api/v1/sessions/{unopened_session_id}/history", json={})
    assert history.status_code == 200, history.text
    assert history.json()["data"]["items"] == [], "通知会话的执行状态必须由后续真实消息产生"
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    interaction = build_cassette(
        asset_id="gateway-user-view-background-notification", model="e2e-stub-model",
        url=f"http://127.0.0.1:{port_block.port(10)}/v1/chat/completions",
        frames=chat_completion_frames(response_id="chatcmpl-user-view-background", model="e2e-stub-model",
            deltas=({"role": "assistant", "content": "后台完成通知已生成。"},), finish_reason="stop"),
    ).raw["interactions"][0]
    call_id, = replay.control.plan_model_calls(unopened_session_id, unopened_thread_id, [interaction])
    gateway = start_gateway_process(
        workspace_root=Path(browser_backend[1]),
        default_backend_url=browser_backend[0],
        port=port_block.port(20),
        extra_env={
            "BOXTEAM_WEB_ASSETS": str(
                project_root / "src" / "clients" / "web" / "dist"
            ),
        },
    )
    artifacts = Path(browser_backend[1]).parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "gateway-user-view-sessions-result.json"
    screenshot_path = artifacts / "gateway-user-view-sessions-failure.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    # Gateway 用户注册表跨运行持久；固定 ID 在第二次运行必触发 409，
    # 每次运行生成唯一用户 ID 保证测试隔离。
    user_a_id = f"user-browser-view-a-{uuid4().hex[:8]}"
    user_b_id = f"user-browser-view-b-{uuid4().hex[:8]}"
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            guest = await client.post(
                "/api/gateway/users/guest",
                json={"tracking": {"source": "browser-integration"}},
            )
            assert guest.status_code == 200, guest.text
            workspace_response = await client.get("/api/gateway/workspaces")
            assert workspace_response.status_code == 200, workspace_response.text
            workspace_id = workspace_response.json()["data"]["active_workspace_id"]
            assert isinstance(workspace_id, str) and workspace_id

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
                "BOXTEAM_BROWSER_SESSION_ID": session_id,
                "BOXTEAM_BROWSER_UNOPENED_SESSION_ID": unopened_session_id,
                "BOXTEAM_BROWSER_USER_A_ID": user_a_id,
                "BOXTEAM_BROWSER_USER_B_ID": user_b_id,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary_environment:
            environment.update(temporary_environment)
            browser_result = await asyncio.to_thread(
                subprocess.run,
                ["node", "tests/integration/clients/web/gateway_user_view_sessions.mjs"],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        assert browser_result.returncode == 0, (
            "Gateway 用户视图双浏览器集成失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["guestAccess"] is True
        assert result["ordinaryUserSelection"] is True
        assert result["expiredUserLeaseReacquired"] is True
        assert result["viewRestoredAfterReload"] is True
        assert result["occupiedStateVisible"] is True
        assert result["takeoverCompleted"] is True
        assert result["oldPageExitedAfterTakeover"] is True
        assert result["viewRestoredInSecondBrowser"] is True
        assert result["unopenedSessionUnread"] is True
        assert result["noPrivateActivityContent"] is True
        artifacts = list(replay.artifacts.glob("*.json"))
        assert [path.stem for path in artifacts] == [call_id], "只能执行未打开会话的真实通知消息"
        recorded = json.loads(artifacts[0].read_text())
        assert recorded["identity"] == {"session_id": unopened_session_id, "thread_id": unopened_thread_id, "model_call_id": call_id}
        assert recorded["request"]["stream"] is True
        assert "后台完成通知测试" in json.dumps(recorded["request"]["messages"], ensure_ascii=False)
        compiled = [event for event in replay.control.events if event["phase"] == "agent.graph.compiled"]
        assert len(compiled) == 1
        assert compiled[0]["details"]["session_id"] == unopened_session_id
        assert compiled[0]["details"]["thread_id"] == unopened_thread_id
    finally:
        close_gateway_process(gateway)
