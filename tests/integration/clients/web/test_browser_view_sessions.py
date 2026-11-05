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
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式 Workbench 产物
)
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
    """只准备浏览器视图恢复需要的展示历史；后台通知仍通过真实消息入口执行。"""
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
        context.artifacts_dir / "model-replay", runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="browser-view-isolation-and-real-background-result-notification",
    ) as replay:
        backend = start_backend_process(
            workspace_root=str(workspace_root),
            port=port_block.port(0),
            log_name="browser-view-backend",
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


async def _verify_access_contract(base_url: str) -> dict[str, bool]:
    """已删除账户菜单；保留真实 HTTP 租约合同，不以本地展示态替代认证。"""
    user_a = f"browser-access-a-{uuid4().hex[:8]}"
    user_b = f"browser-access-b-{uuid4().hex[:8]}"
    async with (
        httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=30) as first,
        httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=30) as second,
    ):
        for user_id in (user_a, user_b):
            created = await first.post("/api/gateway/users", json={"user_id": user_id, "display_name": user_id})
            assert created.status_code == 200, created.text
        acquired = await first.post(f"/api/gateway/users/{user_a}/access", json={"client_label": "电脑 A"})
        assert acquired.status_code == 200, acquired.text
        assert acquired.json()["data"]["user_id"] == user_a
        occupied = await second.post(f"/api/gateway/users/{user_a}/access", json={"client_label": "电脑 B"})
        assert occupied.status_code == 409, occupied.text
        assert occupied.json()["detail"]["code"] == "user_lease_occupied"
        users = await second.get("/api/gateway/users")
        assert users.status_code == 200, users.text
        user = next(item for item in users.json()["data"]["items"] if item["user_id"] == user_a)
        assert user["lease"]["occupied"] is True
        takeover = await second.post(f"/api/gateway/users/{user_a}/takeover", json={"client_label": "电脑 B"})
        assert takeover.status_code == 200, takeover.text
        assert takeover.json()["data"]["user_id"] == user_a
        assert takeover.json()["data"]["lease_generation"] == acquired.json()["data"]["lease_generation"] + 1
        previous = await first.get("/api/gateway/users/current")
        assert previous.status_code == 401, previous.text
        assert previous.json()["detail"] == "user_session_required"
        # 浏览器由 ensureGatewayUserAccess 在 401 后显式重建游客，HTTP 本身不会替客户端切换身份。
        guest = await first.post("/api/gateway/users/guest", json={})
        assert guest.status_code == 200, guest.text
        previous = await first.get("/api/gateway/users/current")
        assert previous.status_code == 200, previous.text
        assert previous.json()["data"]["kind"] == "guest"
        assert previous.json()["data"]["user_id"] == guest.json()["data"]["user_id"]
        assert previous.json()["data"]["user_id"] != user_a
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=30) as expired_client:
            expired = await expired_client.post(f"/api/gateway/users/{user_b}/access", json={"client_label": "过期电脑"})
            assert expired.status_code == 200, expired.text
            generation = expired.json()["data"]["lease_generation"]
        # 保留真实 45 秒租约过期，不修改服务端时间、数据库或 TTL。
        await asyncio.sleep(46)
        reacquired = await second.post(f"/api/gateway/users/{user_b}/access", json={"client_label": "恢复电脑"})
        assert reacquired.status_code == 200, reacquired.text
        assert reacquired.json()["data"]["user_id"] == user_b
        assert reacquired.json()["data"]["lease_generation"] == generation + 1
    return {"ordinaryUserAcquisition": True, "occupiedConflict": True,
            "takeoverInvalidatesOldAccess": True, "expiredLeaseReacquired": True}


@pytest.mark.asyncio
async def test_browser_views_restore_locally_and_isolate_browser_contexts(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path, ModelStreamReplay],
    browser_backend_client: httpx.AsyncClient,
    web_workbench_assets: Path,
) -> None:
    project_root = Path.cwd().resolve()
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("浏览器持久视图集成需要 Chromium")
    scope = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    session_id = await _create_session(browser_backend_client, "浏览器本地视图会话")
    unopened_session_id = await _create_session(browser_backend_client, "未打开会话通知")
    _seed_rollout(browser_backend[1], session_id, count=8)
    session = await browser_backend_client.get(f"/api/v1/sessions/{session_id}")
    assert session.status_code == 200, session.text
    session_owner = session.json()["data"]
    main_thread = await browser_backend_client.get(f"/api/v1/sessions/{unopened_session_id}/main-thread")
    assert main_thread.status_code == 200, main_thread.text
    unopened_thread_id = main_thread.json()["data"]["thread_id"]
    history = await browser_backend_client.post(f"/api/v1/sessions/{unopened_session_id}/threads/{unopened_thread_id}/history", json={})
    assert history.status_code == 200, history.text
    assert history.json()["data"]["items"] == [], "通知会话的执行状态必须由后续真实消息产生"
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    replay = browser_backend[2]
    interaction = build_cassette(
        asset_id="browser-view-background-notification", model="e2e-stub-model",
        url=f"http://127.0.0.1:{port_block.port(10)}/v1/chat/completions",
        frames=chat_completion_frames(response_id="chatcmpl-browser-view-background", model="e2e-stub-model",
            deltas=({"role": "assistant", "content": "后台完成通知已生成。"},), finish_reason="stop"),
    ).raw["interactions"][0]
    call_id, = replay.control.plan_model_calls(unopened_session_id, unopened_thread_id, [interaction])
    gateway = start_gateway_process(
        workspace_root=browser_backend[1], default_backend_url=browser_backend[0], port=port_block.port(20),
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)},
    )
    artifacts = scope.artifacts_dir
    result_path = artifacts / "browser-view-sessions-result.json"
    screenshot_path = artifacts / "browser-view-sessions-failure.png"
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=60) as client:
            guest = await client.post("/api/gateway/users/guest", json={"tracking": {"source": "browser-integration"}})
            assert guest.status_code == 200, guest.text
            workspaces = await client.get("/api/gateway/workspaces")
            assert workspaces.status_code == 200, workspaces.text
            active = workspaces.json()["data"]["active_workspace"]
            workspace_id = active["workspace_id"]
            gateway_id = active["gateway_id"]
            assert workspace_id == session_owner["workspace_id"]
            health = await client.get("/api/gateway/health")
            assert health.status_code == 200, health.text
            assert health.json()["data"]["gateway_id"] == gateway_id
        environment = os.environ | {
            "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
            "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
            "BOXTEAM_BROWSER_GATEWAY_ID": gateway_id,
            "BOXTEAM_BROWSER_OWNER": json.dumps(session_owner),
            "BOXTEAM_BROWSER_UNOPENED_SESSION_ID": unopened_session_id,
            "BOXTEAM_BROWSER_UNOPENED_THREAD_ID": unopened_thread_id,
            "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
            "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
        }
        with chromium_temporary_environment(scope.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary_environment:
            browser_result = await asyncio.to_thread(
                subprocess.run, ["bun", "tests/integration/clients/web/browser_view_sessions.mjs"],
                cwd=project_root, env=environment | temporary_environment, capture_output=True, text=True,
                timeout=180, check=False,
            )
        (artifacts / "browser-process.json").write_text(json.dumps({"exit_code": browser_result.returncode}) + "\n", encoding="utf-8")
        (artifacts / "browser.stdout.log").write_text(browser_result.stdout, encoding="utf-8")
        (artifacts / "browser.stderr.log").write_text(browser_result.stderr, encoding="utf-8")
        assert browser_result.returncode == 0, f"浏览器视图集成失败:\n{browser_result.stdout}\n{browser_result.stderr}\n{result_path}\n{screenshot_path}"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["classification"] == "Integration"
        for key in ("guestAccess", "viewRestoredAfterReload", "viewRestoredInNewPage", "browserContextsIsolated",
                    "historyCacheBeforeSync", "historyCacheContextsIsolated", "unopenedSessionUnread",
                    "openedSessionRead", "unreadPagesIsolated",
                    "noPrivateActivityContent", "noServerViewWrites"):
            assert result[key] is True, key
        recovery = result["historyRecovery"]
        assert [item["expectCache"] for item in recovery] == [False, True]
        for item in recovery:
            assert item["sessionId"] == session_id
            assert item["workspaceId"] == session_owner["workspace_id"]
            assert item["threadId"] == session_owner["thread_id"]
            assert item["heldRequests"]
            assert all(held["workspaceId"] == workspace_id and held["gatewayId"] == gateway_id
                       for held in item["heldRequests"])
            assert all(held["threadId"] == session_owner["thread_id"] for held in item["heldRequests"])
            assert item["requestId"]
            assert item["liveAfterSync"] is True
        assert recovery[0]["cachedTurnIds"] == []
        assert recovery[0]["cachedActionsDisabled"] is None
        assert recovery[1]["cachedActionsDisabled"] is True
        assert recovery[1]["cachedControlCount"] > 0
        assert recovery[1]["cachedTurnIds"]
        assert set(recovery[1]["cachedTurnIds"]) <= {f"job-{index:04d}" for index in range(1, 9)}
        assert result["pageErrors"] == []
        read_proof = result["unreadReadProof"]
        assert read_proof["retainedUntilEditorOpened"] is True
        assert read_proof["sessionId"] == unopened_session_id
        assert read_proof["threadId"] == unopened_thread_id
        assert read_proof["requestId"]
        assert read_proof["heldRequests"]
        assert all(item["workspaceId"] == workspace_id and item["gatewayId"] == gateway_id
                   for item in read_proof["heldRequests"])
        search_proof = read_proof["search"]
        assert search_proof["requestId"]
        assert search_proof["workspaceId"] == session_owner["workspace_id"]
        assert search_proof["gatewayId"] == gateway_id
        assert search_proof["sessionId"] == unopened_session_id
        assert search_proof["unreadBeforeEditorOpened"] is True
        access = await _verify_access_contract(f"http://127.0.0.1:{gateway.port}")
        (artifacts / "access-contract.json").write_text(json.dumps(access, indent=2) + "\n", encoding="utf-8")
        recordings = list(replay.artifacts.glob("*.json"))
        assert [path.stem for path in recordings] == [call_id], "只能执行未打开会话的真实通知消息"
        recorded = json.loads(recordings[0].read_text())
        assert recorded["identity"] == {"session_id": unopened_session_id, "thread_id": unopened_thread_id, "model_call_id": call_id}
        assert recorded["request"]["stream"] is True
        assert "后台完成通知测试" in json.dumps(recorded["request"]["messages"], ensure_ascii=False)
        compiled = [event for event in replay.control.events if event["phase"] == "agent.graph.compiled"]
        assert len(compiled) == 1
        assert compiled[0]["details"]["session_id"] == unopened_session_id
        assert compiled[0]["details"]["thread_id"] == unopened_thread_id
    finally:
        close_gateway_process(gateway)
