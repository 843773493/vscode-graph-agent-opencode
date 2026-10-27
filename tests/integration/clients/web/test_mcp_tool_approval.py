"""真实浏览器提交原工具审批，复用 Gateway／MCP／model_stream 资产。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import pytest

from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.agents.test_mcp_tool_approval import (
    approval_plan as approval_plan,  # noqa: PLC0414 - 同一模型响应资产
)
from tests.integration.backend.agents.test_mcp_tool_approval import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 同一真实 HTTP MCP
)
from tests.integration.backend.agents.test_mcp_tool_approval import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 同一未授权兄弟 Server
)
from tests.integration.backend.agents.test_mcp_tool_approval import (
    product_case as product_case,  # noqa: PLC0414 - 同一产品进程装具
)
from tests.integration.backend.agents.test_mcp_tool_approval import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 当前文件正式隔离工作区
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done


@pytest.fixture
def approval_browser():
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "工具审批 Web 验收需要 Chromium"

    async def run(case, *, base_url, title):
        artifacts = case.context.artifacts_dir / "approval-browser"
        artifacts.mkdir()
        command = ["bun", "tests/integration/clients/web/mcp_tool_approval.mjs"]
        result = await asyncio.to_thread(subprocess.run, command, cwd=case.context.project_root,
            timeout=150, check=False, capture_output=True, text=True,
            env={**os.environ, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                 "BOXTEAM_BROWSER_ARTIFACTS": str(artifacts), "BOXTEAM_BROWSER_FIXTURE": json.dumps({
                     "baseUrl": base_url, "sessionTitle": title, "workspaceId": case.mount.workspace_id,
                 })})
        (artifacts / "browser.raw").write_text(result.stdout + result.stderr)
        (artifacts / "receipt.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}))
        assert result.returncode == 0, (artifacts / "browser.raw").read_text()
        return json.loads((artifacts / "approval-result.json").read_text())
    return run


async def test_web_edits_and_rejects_original_tool_calls(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, approval_plan, approval_browser,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("web-approval") as original,
        product_case(secure, original, web=True,
                     tool_restrictions={"confirmation_required": ["mcp__schema__probe"]}) as (client, replay, case),
    ):
        title = "Web 逐调用工具审批"
        session = await create_session(client, title)
        thread = (await client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]
        planned, calls = approval_plan(replay, session, thread)
        result = await approval_browser(case, base_url=str(client.base_url), title=title)
        await wait_for_job_done(client, result["jobId"], max_attempts=60)
        assert [json.loads(line)["arguments"] for line in original.receipts.read_text().splitlines()] == [{"proof": "approved-in-browser"}]
        decisions = result["request"]["responses"][0]["decisions"]
        assert [decision["tool_call_id"] for decision in decisions] == [call["id"] for call in calls]
        records = [json.loads((replay.artifacts / f"{identity}.json").read_text()) for identity in planned]
        assert len(records) == 3
        results = [message for message in records[-1]["request"]["messages"] if message.get("role") == "tool"
                   and message.get("tool_call_id") in {call["id"] for call in calls}]
        assert {message["tool_call_id"] for message in results} == {call["id"] for call in calls}
        assert any("浏览器明确拒绝第二个调用" in str(message["content"]) for message in results)
        with RolloutCheckpointSaver(case.mount.workspace_root / ".boxteam/sessions") as saver:
            for record in records:
                with saver._storage.storage.read_connection(session, thread) as connection:
                    assembly_id, = connection.execute("SELECT assembly_id FROM context_assemblies WHERE model_call_id=?", (record["identity"]["model_call_id"],)).fetchone()
                assembly = saver.get_context_assembly(session, thread, assembly_id=assembly_id)
                assert assembly.request_hash_preimage == normalize_wire_request(record["request"])
                assembly.validate_hashes()
