from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.integration.stubs.http_stubs import openai_chat_stub
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.paths import output_root_for_test
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import close_backend_process, start_backend_process
from tests.support.workspaces import prepare_default_test_workspace

BOUNDARY_CASES = (
    ("ses_b1a2c3d4e5f6478899aabbccddeeff01", "boundary-turn-0001", "completed"),
    ("ses_b1a2c3d4e5f6478899aabbccddeeff02", "boundary-turn-0002", "cancelled"),
    ("ses_b1a2c3d4e5f6478899aabbccddeeff03", "boundary-turn-0003", "failed"),
    ("ses_b1a2c3d4e5f6478899aabbccddeeff04", "boundary-turn-0004", "cancelled"),
    ("ses_b1a2c3d4e5f6478899aabbccddeeff05", "boundary-turn-0005", "completed"),
    ("ses_b1a2c3d4e5f6478899aabbccddeeff06", "boundary-turn-0006", "completed"),
)


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    project_root = Path.cwd().resolve()
    output_root = output_root_for_test(
        Path(request.node.fspath),
        test_layer="integration",
        project_root=project_root,
    )
    workspace_root = prepare_default_test_workspace(
        workspace_root=output_root / "workspace",
        template_root=project_root / "tests" / "fixtures" / "workspaces" / "custom_tool_test_workspace",
        shared_skill_root=project_root / "resources" / "skills",
    )
    return str(workspace_root)


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
    provider = next(item for item in config["llm"]["providers"] if item["id"] == "primary")
    provider.update(
        {
            "endpoint": f"http://127.0.0.1:{port_block.port(10)}/v1",
            "model": "boundary-cases-browser-stub",
            "api_key": "boundary-cases-local-key",
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
            log_name="boundary-cases-browser-backend",
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


@pytest.mark.asyncio
async def test_boundary_fixtures_have_distinct_copilot_style_web_states(
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
