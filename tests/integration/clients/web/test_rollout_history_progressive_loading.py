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

STATIC_LONG_SESSION_ID = "ses_9f4e2c7a1b6d4830a5e8f2c1d7b90436"


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
        template_root=project_root / "asset" / "custom_tool_test_workspace",
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
    provider = next(
        item for item in config["llm"]["providers"] if item["id"] == "primary"
    )
    provider.update(
        {
            "endpoint": f"http://127.0.0.1:{port_block.port(10)}/v1",
            "model": "rollout-history-browser-stub",
            "api_key": "rollout-history-local-key",
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
            log_name="rollout-history-browser-backend",
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
async def test_rollout_history_around_loading_real_web_chain(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path],
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
        pytest.fail("Rollout 历史浏览器集成需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    session_id = STATIC_LONG_SESSION_ID
    static_rollout_root = (
        Path(integration_workspace_root_path)
        / ".boxteam"
        / "sessions"
        / session_id
        / "rollout"
    )
    rollout_path = static_rollout_root / "rollout.jsonl"
    assert rollout_path.is_file()
    assert not list(static_rollout_root.glob("segment-*.jsonl"))
    large_records = []
    for line in rollout_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        message = record.get("message", {})
        data = message.get("data", {}) if isinstance(message, dict) else {}
        tool_calls = data.get("tool_calls", []) if isinstance(data, dict) else []
        if any(
            isinstance(call, dict)
            and isinstance(call.get("args"), dict)
            and isinstance(call["args"].get("arguments"), dict)
            and isinstance(call["args"]["arguments"].get("query_context"), str)
            for call in tool_calls
        ):
            large_records.append(record)
    assert len(large_records) >= 10
    assert any(
        record.get("message", {}).get("data", {}).get("tool_calls", [{}])[0].get("name")
        == "invoke_custom_tool"
        for record in large_records
        if record.get("message", {}).get("data", {}).get("tool_calls")
    )
    assert all(
        "payload_ref" not in record.get("message", {}) for record in large_records
    )
    session_response = await browser_backend_client.get(
        f"/api/v1/sessions/{session_id}"
    )
    assert session_response.status_code == 200, session_response.text

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
    result_path = artifacts / "rollout-history-progressive-result.json"
    screenshot_path = artifacts / "rollout-history-progressive-failure.png"
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
                json={"tracking": {"source": "rollout-history-browser-test"}},
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
                "BOXTEAM_BROWSER_SESSION_ID": session_id,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        browser_result = await asyncio.to_thread(
            subprocess.run,
            [
                "node",
                "tests/integration/clients/web/rollout_history_progressive_loading.mjs",
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        assert browser_result.returncode == 0, (
            "rollout 历史浏览器集成失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["defaultProjectionSafe"] is True
        assert result["canonicalMixedMessageRestored"] is True
        assert result["toolDetailsLoaded"] is True
        assert result["largeToolSummarySafe"] is True
        assert result["largeToolDetailsBounded"] is True
        assert result["aroundOrdinals"] == list(range(60, 69))
        assert result["aroundCursorsPresent"] is True
        assert result["aroundBidirectionalSafe"] is True
        assert result["beforeOrdinals"] == [
            [124, 125, 126, 127],
            [120, 121, 122, 123],
            [116, 117, 118, 119],
        ]
        # SQLite reader 本身远低于该值；这里约束完整浏览器/Gateway 链路，
        # 不把前端渲染预算误当成数据库耗时。
        assert result["historyRequestP95Ms"] < 200
        # 首次真实浏览器 prepend 包含 Virtuoso 首次布局和 Gateway 冷路径；
        # 热路径 API p95 仍由上面的 100ms 断言约束。
        assert result["browserPrependMs"] < 500
        assert result["noPageErrors"] is True
    finally:
        close_gateway_process(gateway)
