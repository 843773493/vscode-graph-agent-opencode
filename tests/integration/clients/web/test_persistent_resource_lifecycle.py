"""真实资源卸载、重启与删除链路；模型调用使用显式精确回放。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from tests.harness.python.browser_manager import (
    close_browser_frontend_process,
    start_browser_frontend_process,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式 Workbench 构建装具
)
from tests.support.model_stream.config import model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.terminal_plugin import enable_developer_tools_plugins


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phases", [("before-unload", "after-unload"), ("workspace-browser",)],
    ids=["unload-restart", "workspace-browser"],
)
async def test_persistent_resources_through_real_workbench(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    web_workbench_assets: Path,
    phases: tuple[str, ...],
) -> None:
    scope = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    project = scope.project_root
    workspace = Path(integration_workspace_root_path)
    artifacts = scope.artifacts_dir
    runtime = scope.runtime_root_for_node(request.node.nodeid)
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if chromium is None:
        pytest.fail("持久资源 Web Integration 需要真实 Chromium")
    ports = integration_port_block_for_file(Path(request.node.path))
    with ExitStack() as lifetime:
        temporary_environment = lifetime.enter_context(chromium_temporary_environment(runtime / "chromium-tmp"))
        replay = lifetime.enter_context(model_stream_replay(
            runtime, runtime_root=runtime, business_assertion="persistent-resource-lifecycle",
        ))
        browser = start_browser_frontend_process(
            workspace_root=workspace, artifacts_dir=artifacts, frontend_port=ports.port(43),
        )
        lifetime.callback(close_browser_frontend_process, browser)
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=ports.port(21),
            boxteam_home=scope.boxteam_home_for_node(request.node.nodeid), artifacts_dir=artifacts / "gateway",
            extra_env=temporary_environment | replay.environment | {
                "BOXTEAM_WEB_ASSETS": str(web_workbench_assets),
                "BOXTEAM_DEFAULT_BACKEND_PORT": str(ports.port(10)),
                "BOXTEAM_BROWSER_FRONTEND_URL": f"http://127.0.0.1:{browser.port}",
            },
            asgi_app="tests.support.e2e_control.gateway_app:app",
        )
        lifetime.callback(close_gateway_process, gateway)
        base_url = f"http://127.0.0.1:{gateway.port}"
        async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=60) as client:
            await acquire_gateway_guest(client)
            reply = await client.get("/api/gateway/workspaces")
            assert reply.status_code == 200, reply.text
            owner = reply.json()["data"]["active_workspace"]
            workspace_id = owner["workspace_id"]
            gateway_id = owner["gateway_id"]
            await enable_developer_tools_plugins(
                client, gateway_id=gateway_id, workspace_id=workspace_id,
                plugin_ids=("terminal",), artifacts_dir=artifacts / "terminal-plugin",
            )
        environment = os.environ | temporary_environment | replay.environment | {
            "BOXTEAM_BROWSER_BASE_URL": base_url, "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
            "BOXTEAM_BROWSER_GATEWAY_ID": gateway_id,
            "BOXTEAM_BROWSER_ARTIFACT_ROOT": str(artifacts), "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
        }
        for phase in phases:
            result = await asyncio.to_thread(
                subprocess.run,
                ["bun", "tests/integration/clients/web/persistent_resource_lifecycle.mjs", phase],
                cwd=project, env=environment, capture_output=True, text=True, timeout=240, check=False,
            )
            (artifacts / f"{phase}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            (artifacts / f"{phase}-process.json").write_text(json.dumps({"exit_code": result.returncode}) + "\n", encoding="utf-8")
            phase_result = artifacts / f"{phase}.json"
            details = phase_result.read_text() if phase_result.is_file() else "阶段报告未生成"
            assert result.returncode == 0, f"{phase}: {result.stdout}\n{result.stderr}\n{details}"
            if phase == "before-unload":
                async with httpx.AsyncClient(base_url=base_url, headers=LOCAL_TOKEN_HEADERS, timeout=120) as client:
                    await acquire_gateway_guest(client)
                    client.headers.update({"X-BoxTeam-Target-Gateway-Id": gateway_id,
                                           "X-BoxTeam-Workspace-Id": workspace_id})
                    health_before = await client.get("/api/v1/health")
                    assert health_before.status_code == 200, health_before.text
                    restarted = await client.post(f"/api/gateway/workspaces/{workspace_id}/runtime/restart-safe")
                    assert restarted.status_code == 200, restarted.text
                    assert restarted.json()["data"]["status"] == "restarted", restarted.text
                    assert restarted.json()["data"]["blockers"] == [], restarted.text
                    health_after = await client.get("/api/v1/health")
                    assert health_after.status_code == 200, health_after.text
                    assert health_before.json()["process_id"] != health_after.json()["process_id"]
                    (artifacts / "backend-restart.json").write_text(json.dumps({
                        "before": health_before.json(), "after": health_after.json(), "restart": restarted.json(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
        facts = json.loads((artifacts / f"{phases[-1]}.json").read_text())
        assert facts["passed"] is True
        if phases == ("workspace-browser",):
            assert facts["workspace_browser_created_and_deleted"] is True
            return
        assert facts["kinds"] == ["terminal", "browser"]
        assert facts["real_model_job_completed"] is True
        assert facts["unload_associations_removed"] is True
        assert facts["other_thread_attach_detach"] is True
        assert facts["session_deleted_resources_accessible"] is True
        assert facts["conflict_shown_and_owner_refetched"] is True
