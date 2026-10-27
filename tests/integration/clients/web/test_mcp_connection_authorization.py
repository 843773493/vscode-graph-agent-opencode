"""真实 Web→Gateway→Agent→OAuth/MCP；模型响应复用 model_stream。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.agents.test_tool_discovery_policy import (
    discovery_plan as discovery_plan,  # noqa: PLC0414 - 复用同一 model_stream 响应资产
)
from tests.integration.backend.agents.test_tool_discovery_policy import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 同一 MCP handler
)
from tests.integration.backend.agents.test_tool_discovery_policy import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 同一真实 OAuth 资产
)
from tests.integration.backend.agents.test_tool_discovery_policy import (
    product_case as product_case,  # noqa: PLC0414 - 真实 Gateway/Backend 产品装具
)
from tests.integration.backend.agents.test_tool_discovery_policy import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 本文件正式隔离工作区
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done


@pytest.fixture
def browser_runner():
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "MCP Web 产品验收需要 Chromium"

    async def run(case, *, base_url, session_title, mode):
        artifacts = case.context.artifacts_dir / f"browser-{mode}"
        artifacts.mkdir()
        command = ["bun", "tests/integration/clients/web/mcp_connection_authorization.mjs"]
        result = await asyncio.to_thread(subprocess.run, command,
            cwd=case.context.project_root, timeout=400 if mode == "errors" else 130, check=False, capture_output=True, text=True,
            env={**os.environ, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                 "BOXTEAM_BROWSER_ARTIFACTS": str(artifacts), "BOXTEAM_BROWSER_FIXTURE": json.dumps({
                     "baseUrl": base_url, "sessionTitle": session_title, "mode": mode,
                     "workspaceId": case.mount.workspace_id,
                 })})
        (artifacts / "browser.raw").write_text(result.stdout + result.stderr, encoding="utf-8")
        (artifacts / "receipt.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}))
        assert result.returncode == 0, (artifacts / "browser.raw").read_text()
        filename = {"authorize": "authorization-result.json", "execute": "execution-result.json", "errors": "connection-errors-result.json"}[mode]
        return json.loads((artifacts / filename).read_text())
    return run


async def test_web_authorization_reaches_original_owner_then_discovers_and_executes(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, discovery_plan, browser_runner,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, web=True) as (client, replay, case),
    ):
        title = "Web 首次 MCP 授权与真实执行"
        session = await create_session(client, title)
        response = await client.get(f"/api/v1/sessions/{session}/main-thread")
        assert response.status_code == 200, response.text
        thread = response.json()["data"]["thread_id"]
        first = discovery_plan(replay, session, thread, [
            ("search_tools", {"tool_ids": ["connections.manage", "mcp__secure__probe"]}),
            ("invoke_extension_tool", {"tool_name": "connections.manage", "arguments": {"request": {"operation": "connect", "server_id": "secure"}}}),
        ], key="web-before-authorization")
        authorized = await browser_runner(case, base_url=str(client.base_url), session_title=title, mode="authorize")
        await wait_for_job_done(client, authorized["jobId"], max_attempts=60)
        assert authorized["backendReady"] and authorized["bottomTabPersisted"] and authorized["privateReferrerAbsent"]
        assert secure.registrations == secure.exchanges == len(secure.authorizations) == 1
        second = discovery_plan(replay, session, thread, [
            ("search_tools", {"tool_ids": ["mcp__secure__probe"]}),
            ("invoke_extension_tool", {"tool_name": "mcp__secure__probe", "arguments": {"proof": "actual-web-oauth-owner"}}),
        ], key="web-after-authorization")
        executed = await browser_runner(case, base_url=str(client.base_url), session_title=title, mode="execute")
        await wait_for_job_done(client, executed["jobId"], max_attempts=60)
        assert [json.loads(line) for line in secure.receipts.read_text().splitlines()] == [
            {"name": "probe", "arguments": {"proof": "actual-web-oauth-owner"}},
        ]
        records = [json.loads((replay.artifacts / f"{identity}.json").read_text())["request"] for identity in first + second]
        found = [message for message in records[-1]["messages"] if message["role"] == "tool"]
        discovered = json.loads(found[-2]["content"])["results"][0]
        assert discovered["schema_state"] == "inline_complete" and discovered["execution_ready"] is True
        secrets = [secure.authorizations[0]["code"], secure.authorizations[0]["state"], secure.access_token, secure.refresh_token]
        assert all(secret not in json.dumps(records) for secret in secrets)
        resolver = get_session_path_resolver(case.mount.workspace_root / ".boxteam/sessions")
        with RolloutCheckpointSaver(resolver.sessions_root) as saver:
            assemblies = saver.list_context_assemblies(session, thread)
            assert len(assemblies) == len(records) == 6
            for assembly, record in zip(assemblies, records, strict=True):
                assert assembly.request_hash_preimage == normalize_wire_request(record)
                assembly.validate_hashes()
            canonical = saver._storage.storage.read_items(session, thread)
            assert all(secret not in json.dumps([item.to_dict() for item in canonical]) for secret in secrets)
        # 私有凭据仅在专用凭据 owner；服务日志不保存原回调 query。
        for log_root in (case.context.artifacts_dir / "gateway", case.mount.workspace_root / ".boxteam/logs"):
            for path in log_root.rglob("*.log"):
                assert all(secret not in path.read_text(errors="replace") for secret in secrets), path.name
