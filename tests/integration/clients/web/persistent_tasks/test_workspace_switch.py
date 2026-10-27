"""真实注册两个工作区，由页面切换并核对任务、Goal 和操作归属。"""

from __future__ import annotations

import asyncio
import json

import httpx

from app.core.resource_uri import thread_resource_uri
from tests.integration.backend.sessions.test_persistent_tasks import _configuration
from tests.integration.clients.web.persistent_tasks.test_management import (
    browser_runner as browser_runner,  # noqa: PLC0414 - 复用唯一浏览器入口
)
from tests.integration.clients.web.persistent_tasks.test_management import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414
)
from tests.integration.clients.web.persistent_tasks.test_management import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414
)
from tests.integration.clients.web.persistent_tasks.test_management import (
    product_case as product_case,  # noqa: PLC0414
)
from tests.integration.clients.web.persistent_tasks.test_management import (
    recovery_case as recovery_case,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


async def _prepare_owner(client: httpx.AsyncClient, workspace_id: str, label: str) -> dict:
    session = await create_session(client, f"工作区 {label} 会话")
    response = await client.get(f"/api/v1/sessions/{session}/main-thread")
    assert response.status_code == 200, response.text
    uri = thread_resource_uri(workspace_id, session, response.json()["data"]["thread_id"])
    response = await client.put(f"/api/v1/sessions/{session}/goal", json={
        "objective": f"工作区 {label} 暂停目标", "status": "paused",
    })
    assert response.status_code == 200, response.text
    configuration = _configuration(uri)
    configuration["name"] = f"工作区 {label} 原任务"
    response = await client.post("/api/v1/persistent-tasks", json={
        "idempotency_key": "same-key-in-independent-workspaces", "configuration": configuration,
    })
    assert response.status_code == 200, response.text
    task = response.json()["data"]
    return {"workspaceId": workspace_id, "sessionId": session,
            "targetUri": task["configuration"]["action"]["target_uri"], "task": task}


async def test_workspace_switch_keeps_task_actions_history_and_goal_with_their_owner(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, browser_runner,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, web=True) as (client, replay, case),
    ):
        listed = (await client.get("/api/gateway/workspaces")).json()["data"]
        primary = next(row for row in listed["items"] if row["backend_workspace_id"] == case.mount.workspace_id)
        client.headers["X-BoxTeam-Workspace-Id"] = primary["workspace_id"]
        first = await _prepare_owner(client, case.mount.workspace_id, "A")
        secondary_root = await asyncio.to_thread(prepare_default_test_workspace,
            workspace_root=case.context.workspace_root / "secondary",
            template_root=case.context.project_root / "tests/fixtures/workspaces/default_test_workspace")
        (secondary_root / ".boxteam/workspace-identity.json").unlink(missing_ok=True)
        install_test_workspace_config(workspace_root=secondary_root,
            config_path=case.mount.workspace_root / ".boxteam/workspace.jsonc",
            schema_path=case.context.project_root / "configs/workspace_schema.jsonc")
        response = await client.post("/api/gateway/workspaces/local", json={
            "root_path": str(secondary_root), "name": "持久任务第二工作区",
        })
        assert response.status_code == 200, response.text
        secondary, = [row for row in response.json()["data"]["items"] if row["workspace_id"] != primary["workspace_id"]]
        headers = dict(client.headers)
        headers["x-boxteam-workspace-id"] = secondary["workspace_id"]
        headers["x-workspace-id"] = secondary["backend_workspace_id"]
        async with httpx.AsyncClient(base_url=client.base_url, headers=headers, cookies=client.cookies, timeout=30) as second_client:
            second = await _prepare_owner(second_client, secondary["backend_workspace_id"], "B")
            second["routeId"] = secondary["workspace_id"]
            result = await browser_runner(case, client, title="工作区 A 会话",
                target_uri=first["targetUri"], other_session_id=second["sessionId"], script="workspace_switch",
                environment={"BOXTEAM_SECONDARY_WORKSPACE": json.dumps(second)})
            first_after = (await client.get(f"/api/v1/persistent-tasks/{first['task']['task_id']}")).json()["data"]
            second_after = (await second_client.get(f"/api/v1/persistent-tasks/{second['task']['task_id']}")).json()["data"]
            assert first_after["configuration"] == first["task"]["configuration"]
            assert first_after["lifecycle"] == "paused"
            assert second_after["configuration"]["name"] == "工作区 B 编辑任务"
            assert second_after["lifecycle"] == "paused"
            assert (await client.get(f"/api/v1/persistent-tasks/{first['task']['task_id']}/runs")).json()["data"]["items"] == []
            runs = (await second_client.get(f"/api/v1/persistent-tasks/{second['task']['task_id']}/runs")).json()["data"]["items"]
            assert len(runs) == 2 and all(row["task_id"] == second["task"]["task_id"] for row in runs)
            trial, = [row for row in runs if row["check_only"]]
            delivered, = [row for row in runs if not row["check_only"]]
            assert trial["phase"] == "checked" and trial["delivery"] is None
            assert delivered["phase"] == "delivered" and delivered["delivery"]["state"] == "saved"
            assert result["secondTaskId"] == second_after["task_id"]
            assert list(replay.artifacts.glob("*.json")) == []
