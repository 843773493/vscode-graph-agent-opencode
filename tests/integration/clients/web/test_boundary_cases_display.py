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

BOUNDARY_CASES = (
    ("ses_019bf985d867717182fe710be0f6adae", "boundary-turn-0001", "completed"),
    ("ses_019bed0e9f8f7b27a213abe862c0b31a", "boundary-turn-0002", "cancelled"),
    ("ses_019bc1148cec71b78b1aaa3b19b60363", "boundary-turn-0003", "failed"),
    ("ses_019c5123cafb789e8fdf8704f6464f74", "boundary-turn-0004", "cancelled"),
    ("ses_019bdcfcb97f7dab8f30a959418a1658", "boundary-turn-0005", "completed"),
    ("ses_019b7a50d46c7e7f86c9148c7069cf97", "boundary-turn-0006", "completed"),
)


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
        session_ids=tuple(case[0] for case in BOUNDARY_CASES),
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
        business_assertion="read-only-boundary-cases-display",
    ) as replay:
        backend = start_backend_process(
            workspace_root=str(workspace_root),
            port=port_block.port(0),
            log_name="boundary-cases-browser-backend",
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
async def test_boundary_fixtures_have_distinct_copilot_style_web_states(
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
        pytest.fail("边界状态浏览器集成需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    for session_id, turn_id, expected_status in BOUNDARY_CASES:
        response = await browser_backend_client.post(
            f"/api/v1/sessions/{session_id}/history",
            json={
                "turn_ids": [turn_id],
                "include": ["user", "text", "reasoning_detail", "tool_summary", "final_response"],
            },
        )
        assert response.status_code == 200, response.text
        item = response.json()["data"]["items"][0]
        assert item["status"] == expected_status
        if turn_id == "boundary-turn-0006":
            expected = [
                ("read_file", "model-call-boundary-turn-0006:tool-call:call-boundary-parallel-read"),
                ("search_files", "model-call-boundary-turn-0006:tool-call:call-boundary-parallel-search"),
            ]
            for kind in ("tool_call", "tool_result"):
                assert [
                    (part["tool_name"], part["tool_call_id"])
                    for part in item["response_parts"] if part["kind"] == kind
                ] == expected

    port_block = integration_port_block_for_file(Path(request.node.fspath))
    gateway = start_gateway_process(
        workspace_root=Path(browser_backend[1]),
        default_backend_url=browser_backend[0],
        port=port_block.port(20),
        extra_env={
            "BOXTEAM_WEB_ASSETS": str(project_root / "src" / "clients" / "web" / "dist"),
        },
    )
    artifacts = Path(browser_backend[1]).parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "boundary-cases-display-result.json"
    screenshot_path = artifacts / "boundary-cases-display-failure.png"
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
                json={"tracking": {"source": "boundary-cases-browser-test"}},
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
        context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary_environment:
            environment.update(temporary_environment)
            browser_result = await asyncio.to_thread(
                subprocess.run,
                ["node", "tests/integration/clients/web/boundary_cases_display.mjs"],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        assert browser_result.returncode == 0, (
            "边界状态浏览器集成失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert len(result["cases"]) == len(BOUNDARY_CASES)
        assert all(item["finalTextVisible"] for item in result["cases"])
        assert all(item["statusVisible"] for item in result["cases"])
        assert all(item["forbiddenTextsAbsent"] for item in result["cases"])
        assert all(item["expandedTextsVisible"] for item in result["cases"])
        assert result["noPageErrors"] is True
    finally:
        close_gateway_process(gateway)
