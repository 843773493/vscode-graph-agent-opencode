from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.gateway.workspace_ids import build_managed_local_workspace_id
from tests.support.api_waiters import wait_for_job_done
from tests.support.ports import e2e_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.session_navigation.http_operations import submit_navigation_operation
from tests.support.workspaces import prepare_test_workspace


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
        default_backend_url=None,
        port=port,
        extra_env={"BOXTEAM_WEB_ASSETS": str(project_root / "src" / "clients" / "web" / "dist")},
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            await acquire_gateway_guest(client)
            workspaces_response = await client.get("/api/gateway/workspaces")
            assert workspaces_response.status_code == 200, workspaces_response.text
            parent_workspace_id = workspaces_response.json()["data"][
                "active_workspace_id"
            ]
            assert any(
                item["workspace_id"] == parent_workspace_id
                for item in workspaces_response.json()["data"]["items"]
            )
            child_workspace_root = prepare_test_workspace(
                workspace_root=workspace_root / "secondary-workspace",
                template_root=(
                    project_root
                    / "tests"
                    / "fixtures"
                    / "workspaces"
                    / "default_test_workspace"
                ),
                template_items=(),
            )
            add_workspace_response = await client.post(
                "/api/gateway/workspaces/local",
                json={
                    "root_path": str(child_workspace_root),
                    "name": "拖放子工作区",
                },
            )
            assert add_workspace_response.status_code == 200, add_workspace_response.text
            child_workspace = next(
                item
                for item in add_workspace_response.json()["data"]["items"]
                if item["workspace_id"]
                == build_managed_local_workspace_id(str(child_workspace_root))
            )
            assert child_workspace["managed"] is True
            child_workspace_id = child_workspace["workspace_id"]
            parent_response = await client.post(
                "/api/v1/sessions",
                json={"title": "物理父会话"},
            )
            child_response = await client.post(
                "/api/v1/sessions",
                json={"title": "待绑定子会话"},
            )
            folder_receipt = await submit_navigation_operation(
                client,
                "create_folder",
                name="归档文件夹",
            )
            assert parent_response.status_code == 200, parent_response.text
            assert child_response.status_code == 200, child_response.text
            assert folder_receipt["state"] == "committed"
            session_tree_browser_input = {
                "parentSessionId": parent_response.json()["data"]["session_id"],
                "childSessionId": child_response.json()["data"]["session_id"],
                "folderId": str(folder_receipt["created_node_id"]),
                "parentWorkspaceId": parent_workspace_id,
                "childWorkspaceId": child_workspace_id,
            }
            seed_response = await client.post(
                f"/api/v1/sessions/{session_tree_browser_input['parentSessionId']}/messages",
                json={
                    "message": {"content": "请记住目录测试标记 TREE-FORK，只回复这个标记。"},
                    "run": {"mode": "single_agent", "agent_id": "default"},
                },
            )
            assert seed_response.status_code == 200, seed_response.text
            await wait_for_job_done(client, seed_response.json()["data"]["job_id"])

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_E2E_BASE_URL": f"http://127.0.0.1:{port}",
                "BOXTEAM_E2E_FIXTURE": json.dumps(
                    session_tree_browser_input, ensure_ascii=False
                ),
                "BOXTEAM_E2E_RESULT_PATH": str(result_path),
                "BOXTEAM_E2E_SCREENSHOT_PATH": str(screenshot_path),
                "BOXTEAM_E2E_FORK_ARTIFACT_ROOT": str(artifacts),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        result = await asyncio.to_thread(
            subprocess.run,
            ["bun", "tests/e2e/clients/web/session_physical_tree.mjs"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            start_new_session=os.name == "posix",
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, (
            f"浏览器会话物理树 E2E 失败:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}\n截图: {screenshot_path}"
        )
        browser_result = json.loads(result_path.read_text(encoding="utf-8"))
        assert browser_result["bloodlineTextPresent"] is False
        assert browser_result["sessionMainActionOpenedCatalogSession"] is True
        assert browser_result["folderMainActionToggledExpansion"] is True
        assert browser_result["nestedWorkspaceFolders"] is True
        assert browser_result["childWorkspaceMoved"] is True
        assert browser_result["workspaceFolderReturnedToRoot"] is True
        assert browser_result["workspaceExpansionPreserved"] is True
        assert browser_result["phantomSessionEmptyStatePresent"] is False
        assert browser_result["workspaceRootUsesContextMenu"] is True
        assert browser_result["workspaceFolderRecursiveDelete"] is True
        assert browser_result["workspaceMenuCreatedSessionFolder"] is True
        assert browser_result["forkDurableHistory"]["threadId"] == browser_result["forkThreadId"]
        assert browser_result["forkDurableHistory"]["turnCount"] > 0
        assert browser_result["warmDialogBackground"] not in {
            "rgb(255, 255, 255)",
            "rgba(255, 255, 255, 1)",
        }

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            await acquire_gateway_guest(client)
            sessions_response = await client.get("/api/v1/sessions", params={"limit": 100})
            assert sessions_response.status_code == 200, sessions_response.text
            remaining = {
                item["session_id"]: item
                for item in sessions_response.json()["data"]["items"]
            }
            assert session_tree_browser_input["parentSessionId"] not in remaining
            deleted_thread_response = await client.post(
                f"/api/v1/sessions/{session_tree_browser_input['parentSessionId']}"
                f"/threads/{browser_result['forkThreadId']}/history",
                json={},
            )
            assert deleted_thread_response.status_code == 404, deleted_thread_response.text
            assert session_tree_browser_input["childSessionId"] in remaining
            assert (
                remaining[session_tree_browser_input["childSessionId"]]["parent_session_id"]
                is None
            )
            breadcrumb_response = await client.get(
                f"/api/v1/session-catalog/breadcrumb/{session_tree_browser_input['childSessionId']}"
            )
            assert breadcrumb_response.status_code == 200, breadcrumb_response.text
            breadcrumb_ids = [
                item["node_id"]
                for item in breadcrumb_response.json()["data"]["items"]
            ]
            assert breadcrumb_ids == [
                session_tree_browser_input["folderId"],
                session_tree_browser_input["childSessionId"],
            ]
    finally:
        close_gateway_process(gateway)
