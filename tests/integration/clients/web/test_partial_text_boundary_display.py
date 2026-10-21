from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.workspace_identity import read_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.support.model_stream_config import ModelStreamReplay, model_stream_replay
from tests.support.paths import chromium_temporary_environment, output_root_for_test
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)
from tests.support.web_boundary_seeding import prepare_boundary_test_workspace

PARTIAL_TEXT_SESSION_ID = "ses_019c5123cafb789e8fdf8704f6464f74"
PARTIAL_TEXT_TURN_ID = "boundary-turn-0004"
PARTIAL_TEXT_ITEM_ID = "item-boundary-partial-text-0004"
PARTIAL_TEXT_CONTENT = "我已经开始分析这个问题，但回答在这里被用户中断……"


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    project_root = Path.cwd().resolve()
    output_root = output_root_for_test(
        Path(request.node.fspath),
        test_layer="integration",
        project_root=project_root,
    )
    reset_gateway_persistent_state(workspace_root=output_root / "workspace")
    workspace_root = prepare_boundary_test_workspace(
        workspace_root=output_root / "workspace",
        template_root=project_root / "tests" / "fixtures" / "workspaces" / "custom_tool_test_workspace",
        session_ids=(PARTIAL_TEXT_SESSION_ID,),
    )
    return str(workspace_root)


@pytest.fixture(scope="module")
def browser_backend(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> Generator[tuple[str, Path, ModelStreamReplay], None, None]:
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    workspace_root = Path(integration_workspace_root_path).resolve()
    assert Path(integration_workspace_config_path).is_file()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with model_stream_replay(
        context.output_root, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="read-only-partial-text-boundary-display",
    ) as replay:
        backend = start_backend_process(
            workspace_root=str(workspace_root),
            port=port_block.port(0),
            log_name="partial-text-boundary-browser-backend",
            asgi_app="tests.support.e2e_control.backend_app:app",
            env_overrides={
                **replay.environment,
                "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{port_block.port(20)}",
            },
        )
        try:
            yield f"http://127.0.0.1:{backend.port}", workspace_root, replay
        finally:
            close_backend_process(backend)
        assert list(replay.artifacts.glob("*.json")) == [], "只读历史展示不得调用模型"
        assert not any(event["phase"] == "agent.graph.compiled" for event in replay.control.events), "只读历史展示不得物化 graph"


@pytest.fixture
async def browser_backend_client(
    browser_backend: tuple[str, Path, ModelStreamReplay],
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=browser_backend[0],
        headers={
            "X-Local-Token": "local-dev-token",
            "X-BoxTeam-Workspace-Id": read_workspace_id(browser_backend[1]),
        },
        timeout=60,
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_partial_text_cancelled_turn_is_explicit_in_real_web_chain(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path, ModelStreamReplay],
    browser_backend_client: httpx.AsyncClient,
    integration_workspace_root_path: str,
) -> None:
    project_root = Path.cwd().resolve()
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("partial text 浏览器集成需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    sessions_dir = Path(integration_workspace_root_path) / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_dir)
    rollout_path = (
        resolver.resolve_thread_node(
            PARTIAL_TEXT_SESSION_ID,
            resolver.main_thread_id(PARTIAL_TEXT_SESSION_ID),
        )
        / "rollout"
        / "rollout.jsonl"
    )
    boundary_record = next(
        record
        for record in map(json.loads, rollout_path.read_text(encoding="utf-8").splitlines())
        if record.get("turn_id") == PARTIAL_TEXT_TURN_ID
        and record.get("item_id") == PARTIAL_TEXT_ITEM_ID
    )
    assert boundary_record["format_version"] == 2
    assert boundary_record["record_type"] == "item"
    assert boundary_record["semantic_kind"] == "assistant_output"
    assert boundary_record["payload_kind"] == "text"
    assert boundary_record["status"] == "partial"
    assert boundary_record["wire_role"] == "assistant"
    assert boundary_record["payload"] == PARTIAL_TEXT_CONTENT
    assert boundary_record["projection_identity"]["completion_reason"] == "user_interrupt"
    assert "role" not in boundary_record
    assert "message" not in boundary_record

    history_response = await browser_backend_client.post(
        f"/api/v1/sessions/{PARTIAL_TEXT_SESSION_ID}/history",
        json={
            "turn_ids": [PARTIAL_TEXT_TURN_ID],
            "include": ["user", "text", "assistant_text", "final_response"],
        },
    )
    assert history_response.status_code == 200, history_response.text
    history_item = history_response.json()["data"]["items"][0]
    assert history_item["status"] == "cancelled"
    assert history_item["final_response"] == ""
    assert history_item["assistant_text"] == [PARTIAL_TEXT_CONTENT]
    partial_response_part = next(
        part
        for part in history_item["response_parts"]
        if part.get("text") == PARTIAL_TEXT_CONTENT
    )
    assert partial_response_part["kind"] == "text"
    assert partial_response_part["final"] is False

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
    result_path = artifacts / "partial-text-boundary-display-result.json"
    screenshot_path = artifacts / "partial-text-boundary-display-failure.png"
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
                json={"tracking": {"source": "partial-text-boundary-browser-test"}},
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
                "BOXTEAM_BROWSER_SESSION_ID": PARTIAL_TEXT_SESSION_ID,
                "BOXTEAM_BROWSER_TURN_ID": PARTIAL_TEXT_TURN_ID,
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
                [
                    "node",
                    "tests/integration/clients/web/partial_text_boundary_display.mjs",
                ],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        assert browser_result.returncode == 0, (
            "partial text 浏览器集成失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["apiCancelled"] is True
        assert result["apiNonFinalText"] is True
        assert result["partialTextVisible"] is True
        assert result["cancelledStatusVisible"] is True
        assert result["independentRetryVisible"] is False
        assert result["failedRetryLabelVisible"] is False
        assert result["renderErrorVisible"] is False
        assert result["noPageErrors"] is True
    finally:
        close_gateway_process(gateway)
