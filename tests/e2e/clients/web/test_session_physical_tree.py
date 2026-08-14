from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import e2e_port_block_for_file


@pytest.mark.asyncio
async def test_session_tree_context_menus_drive_physical_hierarchy(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
) -> None:
    project_root = Path.cwd().resolve()
    workspace_root = Path(e2e_workspace_root_path).resolve()
    output_root = workspace_root.parent
    artifacts = output_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "session-physical-tree-result.json"
    screenshot_path = artifacts / "session-physical-tree-failure.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)

    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("会话物理树 Web E2E 需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    port = e2e_port_block_for_file(Path(request.node.fspath)).port(20)
    gateway = start_gateway_process(
        workspace_root=workspace_root,
        default_backend_url="http://127.0.0.1:9",
        port=port,
        extra_env={"BOXTEAM_WEB_ASSETS": str(project_root / "src" / "clients" / "web" / "dist")},
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            workspaces_response = await client.get("/api/gateway/workspaces")
            assert workspaces_response.status_code == 200, workspaces_response.text
            parent_workspace_id = workspaces_response.json()["data"][
                "active_workspace_id"
            ]
            parent_workspace = next(
                item
                for item in workspaces_response.json()["data"]["items"]
                if item["workspace_id"] == parent_workspace_id
            )
            secondary_workspace = workspace_root / "secondary-workspace"
            secondary_workspace.mkdir(parents=True, exist_ok=True)
            add_workspace_response = await client.post(
                "/api/gateway/workspaces/local",
                json={
                    "root_path": str(secondary_workspace),
                    "name": "拖放子工作区",
                    "backend_url": parent_workspace["backend_url"],
                },
            )
            assert add_workspace_response.status_code == 200, add_workspace_response.text
            child_workspace_id = next(
                item["workspace_id"]
                for item in add_workspace_response.json()["data"]["items"]
                if Path(item["root_path"]).resolve() == secondary_workspace
            )
            parent_response = await client.post(
                "/api/v1/sessions",
                json={"title": "物理父会话"},
            )
            child_response = await client.post(
                "/api/v1/sessions",
                json={"title": "待绑定子会话"},
            )
            folder_response = await client.post(
                "/api/v1/session-catalog/folders",
                json={"name": "归档文件夹"},
            )
            assert parent_response.status_code == 200, parent_response.text
            assert child_response.status_code == 200, child_response.text
            assert folder_response.status_code == 200, folder_response.text
            fixture = {
                "parentSessionId": parent_response.json()["data"]["session_id"],
                "childSessionId": child_response.json()["data"]["session_id"],
                "folderId": folder_response.json()["data"]["items"][-1]["node_id"],
                "parentWorkspaceId": parent_workspace_id,
                "childWorkspaceId": child_workspace_id,
            }

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_E2E_BASE_URL": f"http://127.0.0.1:{port}",
                "BOXTEAM_E2E_FIXTURE": json.dumps(fixture, ensure_ascii=False),
                "BOXTEAM_E2E_RESULT_PATH": str(result_path),
                "BOXTEAM_E2E_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        result = await asyncio.to_thread(
            subprocess.run,
            ["node", "tests/e2e/clients/web/session_physical_tree.mjs"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, (
            f"浏览器会话物理树 E2E 失败:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}\n截图: {screenshot_path}"
        )
        browser_result = json.loads(result_path.read_text(encoding="utf-8"))
        assert browser_result["bloodlineTextPresent"] is False
        assert browser_result["sessionRowActionCount"] == 0
        assert browser_result["folderRowActionCount"] == 0
        assert browser_result["nestedWorkspaceFolders"] is True
        assert browser_result["childWorkspaceMoved"] is True
        assert browser_result["workspaceFolderReturnedToRoot"] is True
        assert browser_result["workspaceExpansionPreserved"] is True
        assert browser_result["phantomSessionEmptyStatePresent"] is False
        assert browser_result["workspaceRootUsesContextMenu"] is True
        assert browser_result["workspaceFolderRecursiveDelete"] is True
        assert browser_result["workspaceMenuCreatedSessionFolder"] is True
        assert browser_result["warmDialogBackground"] not in {
            "rgb(255, 255, 255)",
            "rgba(255, 255, 255, 1)",
        }

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            sessions_response = await client.get("/api/v1/sessions", params={"limit": 100})
            assert sessions_response.status_code == 200, sessions_response.text
            remaining = {
                item["session_id"]: item
                for item in sessions_response.json()["data"]["items"]
            }
            assert fixture["parentSessionId"] not in remaining
            assert browser_result["forkSessionId"] not in remaining
            assert fixture["childSessionId"] in remaining
            assert remaining[fixture["childSessionId"]]["parent_session_id"] is None
            breadcrumb_response = await client.get(
                f"/api/v1/session-catalog/breadcrumb/{fixture['childSessionId']}"
            )
            assert breadcrumb_response.status_code == 200, breadcrumb_response.text
            breadcrumb_ids = [
                item["node_id"]
                for item in breadcrumb_response.json()["data"]["items"]
            ]
            assert breadcrumb_ids == [fixture["folderId"], fixture["childSessionId"]]
    finally:
        close_gateway_process(gateway)
