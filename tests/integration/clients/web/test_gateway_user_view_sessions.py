from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import commentjson
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from tests.integration.stubs.http_stubs import openai_chat_stub
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import close_backend_process, start_backend_process


def _checkpoint(
    checkpoint_id: str,
    messages: list[object],
    *,
    channel_version: int,
) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {
        "messages": f"{channel_version:032d}.fixture"
    }
    checkpoint["updated_channels"] = ["messages"]
    return checkpoint


def _turn_messages(turn_index: int) -> list[object]:
    turn_id = f"job-{turn_index:04d}"
    stamp = (
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=turn_index)
    ).isoformat()
    user = HumanMessage(
        id=f"user-{turn_index:04d}",
        content=f"用户问题 {turn_index}",
        response_metadata={
            "message_id": f"user-{turn_index:04d}",
            "created_at": stamp,
            "updated_at": stamp,
            "message_metadata": {"turn_id": turn_id, "job_id": turn_id},
        },
    )
    call = AIMessage(
        id=f"assistant-tool-{turn_index:04d}",
        content="",
        tool_calls=[
            {
                "name": "read_fixture",
                "args": {"path": f"fixture/{turn_index:04d}.json"},
                "id": f"call-{turn_index:04d}",
            }
        ],
    )
    result = ToolMessage(
        id=f"tool-result-{turn_index:04d}",
        content=json.dumps(
            {"turn": turn_index, "result": "fixture result"},
            ensure_ascii=False,
        ),
        name="read_fixture",
        tool_call_id=f"call-{turn_index:04d}",
    )
    final = AIMessage(
        id=f"assistant-final-{turn_index:04d}",
        content=f"模型最终响应 {turn_index}",
        response_metadata={"created_at": stamp, "updated_at": stamp},
    )
    return [user, call, result, final]


def _seed_rollout(workspace_root: Path, session_id: str, count: int) -> None:
    saver = RolloutCheckpointSaver(workspace_root / ".boxteam" / "sessions")
    config = build_checkpoint_config(session_id)
    messages: list[object] = []
    for turn_index in range(1, count + 1):
        messages.extend(_turn_messages(turn_index))
        config = saver.put(
            config,
            _checkpoint(
                f"{turn_index:032x}",
                messages,
                channel_version=turn_index,
            ),
            {
                "source": "browser-user-view-stub",
                "step": turn_index,
                "turn": turn_index,
            },
            {"messages": str(turn_index)},
        )
    get_session_path_resolver(
        workspace_root / ".boxteam" / "sessions"
    ).resolve_session_node(session_id)


@pytest.fixture(scope="module")
def browser_backend(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> Generator[tuple[str, Path], None, None]:
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
            "model": "e2e-stub-model",
            "api_key": "e2e-local-model-key",
            "custom_llm_provider": "openai",
        }
    )
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with openai_chat_stub(port_block.port(10)):
        backend = start_backend_process(
            workspace_root=str(workspace_root),
            port=port_block.port(0),
            log_name="gateway-user-view-backend",
        )
        try:
            yield f"http://127.0.0.1:{backend.port}", workspace_root
        finally:
            close_backend_process(backend)


@pytest.fixture
async def browser_backend_client(
    browser_backend: tuple[str, Path],
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=browser_backend[0],
        headers={"X-Local-Token": "local-dev-token"},
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
    browser_backend: tuple[str, Path],
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
    _seed_rollout(Path(browser_backend[1]), unopened_session_id, count=2)

    port_block = integration_port_block_for_file(Path(request.node.fspath))
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
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
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
    finally:
        close_gateway_process(gateway)
