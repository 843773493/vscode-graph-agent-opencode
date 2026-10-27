"""真实页面创建任务，再核对唯一 owner、试运行与并发冲突。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import pytest

from app.core.resource_uri import thread_resource_uri
from tests.integration.backend.agents.test_mcp_connection_control import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 原产品装具依赖
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 原产品装具依赖
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    product_case as product_case,  # noqa: PLC0414 - 真实Gateway与工作区Backend
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 当前文件正式隔离目录
)
from tests.support.api_helpers import create_session


@pytest.fixture
def browser_runner():
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "持久任务Web验收需要Chromium"

    async def run(case, client, *, target_uri, title, other_session_id, script="management", environment=None):
        artifacts = case.context.artifacts_dir / "browser"
        artifacts.mkdir()
        command = ["bun", f"tests/integration/clients/web/persistent_tasks/{script}.mjs"]
        result = await asyncio.to_thread(subprocess.run, command, cwd=case.context.project_root,
            timeout=130, check=False, capture_output=True, text=True,
            env={**os.environ, **(environment or {}), "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                 "BOXTEAM_BROWSER_ARTIFACTS": str(artifacts), "BOXTEAM_BROWSER_FIXTURE": json.dumps({
                     "baseUrl": str(client.base_url), "workspaceId": case.mount.workspace_id,
                     "targetUri": target_uri, "sessionTitle": title, "otherSessionId": other_session_id,
                 })})
        (artifacts / "browser.raw").write_text(result.stdout + result.stderr, encoding="utf-8")
        (artifacts / "receipt.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}))
        assert result.returncode == 0, (artifacts / "browser.raw").read_text()
        return json.loads((artifacts / "actual.json").read_text())
    return run


async def test_workspace_task_form_controls_trial_delivery_and_conflict_readback(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, browser_runner,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, web=True) as (client, replay, case),
    ):
        title = "持久任务Web目标"
        session = await create_session(client, title)
        response = await client.get(f"/api/v1/sessions/{session}/main-thread")
        assert response.status_code == 200, response.text
        thread = response.json()["data"]["thread_id"]
        response = await client.put(f"/api/v1/sessions/{session}/goal", json={"objective": "同一owner的暂停目标", "status": "paused"})
        assert response.status_code == 200, response.text
        goal = response.json()["data"]
        other_session = await create_session(client, "无关联任务的另一个会话")
        before_preview = await client.get("/api/v1/session-catalog/export")
        assert before_preview.status_code == 200, before_preview.text
        result = await browser_runner(case, client, target_uri=thread_resource_uri(case.mount.workspace_id, session, thread), title=title, other_session_id=other_session)
        assert result["preview"]["physical_location"] == "unallocated"
        after_preview = await client.get("/api/v1/session-catalog/export")
        assert after_preview.status_code == 200, after_preview.text
        assert after_preview.json()["data"] == before_preview.json()["data"]
        task_id = result["taskId"]
        response = await client.get(f"/api/v1/persistent-tasks/{task_id}")
        assert response.status_code == 200, response.text
        task = response.json()["data"]
        assert task["lifecycle"] == "terminated" and task["configuration"]["name"] == "另一客户端修改的任务"
        assert task["definition_revision"] == 3
        assert task["configuration"]["condition"] == {"type_id": "builtin.rule", "version": "1", "script": None, "model": None, "rule": {"pointer": "/trigger/type", "expected": "manual"}}
        assert task["configuration"]["context"]["content"] == "浏览器编辑后的提示正文"
        response = await client.get(f"/api/v1/persistent-tasks/{task_id}/runs")
        assert response.status_code == 200, response.text
        runs = response.json()["data"]["items"]
        assert len(runs) == 2
        trial, = [run for run in runs if run["check_only"]]
        delivered, = [run for run in runs if not run["check_only"]]
        assert trial["phase"] == "checked" and trial["decision"]["decision"] == "proceed" and trial["delivery"] is None
        assert delivered["phase"] == "delivered" and delivered["decision"]["decision"] == "proceed"
        assert delivered["delivery"]["state"] == "saved" and delivered["delivery"]["execution_id"] is None
        updated_goal = (await client.get(f"/api/v1/sessions/{session}/goal")).json()["data"]
        assert updated_goal["goal_id"] == goal["goal_id"] and updated_goal["revision"] == goal["revision"] + 1
        assert updated_goal["objective"] == "同一owner的编辑目标" and updated_goal["status"] == "paused"
        # 本次只有上下文发布与纯规则判断，不应出现模型调用或伪用户消息。
        assert list(replay.artifacts.glob("*.json")) == []
        (case.context.artifacts_dir / "actual-owner.json").write_text(json.dumps({"task": task, "runs": runs, "goal": goal}, ensure_ascii=False, indent=2))
