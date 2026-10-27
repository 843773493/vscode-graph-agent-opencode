"""真实 Gateway／Agent 生成长授权等待，完成后由原 thread 独立执行继续。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest

from app.abstractions.mcp_connection_wait import mcp_wait_task_id
from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.browser_manager import (
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.integration.backend.agents.test_debug_prompt_flow import _tool_call
from tests.integration.backend.agents.test_mcp_connection_control import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 复用真实 MCP
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 复用真实 OAuth
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    product_case as product_case,  # noqa: PLC0414 - Gateway／Backend 产品装具
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 本文件正式隔离工作区
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.processes.workspace import start_backend_process, terminate_process


@pytest.fixture
async def wait_mount(recovery_case, free_tcp_port_factory):
    terminal_port, browser_port, browser_frontend_port = (free_tcp_port_factory() for _ in range(3))
    root, artifacts = recovery_case.mount.workspace_root, recovery_case.context.artifacts_dir
    terminal = await asyncio.to_thread(start_terminal_backend, backend_port=terminal_port, workspace_root=str(root), artifacts_dir=artifacts)
    try:
        browser = await asyncio.to_thread(
            start_browser_manager_processes, workspace_root=root, artifacts_dir=artifacts,
            backend_port=browser_port, frontend_port=browser_frontend_port, backend_host="127.0.0.1",
        )
        try:
            yield replace(recovery_case.mount, terminal_backend_url=f"http://127.0.0.1:{terminal_port}", browser_backend_url=f"http://127.0.0.1:{browser_port}")
        finally:
            await asyncio.to_thread(close_browser_manager_processes, browser)
    finally:
        await asyncio.to_thread(terminate_process, terminal)


@pytest.fixture
def wait_plan():
    def plan(replay, session, thread, continuation, operation_id=None):
        request = {"operation": "connect", "server_id": "secure", "continuation": continuation} if operation_id is None else {
            "operation": "status", "operation_id": operation_id, "continuation": continuation,
        }
        operations = [
            ("invoke_extension_tool", {"tool_name": "connections.manage", "arguments": {"request": request}}),
            None,
            ("search_tools", {"tool_ids": ["mcp__secure__probe"]}),
            ("invoke_extension_tool", {"tool_name": "mcp__secure__probe", "arguments": {"proof": "authorized-continuation"}}),
            None,
        ]
        interactions = []
        for index, operation in enumerate(operations):
            delta = {"content": "已登记等待，结束本次执行。" if index == 1 else "已从授权后独立执行继续。"}
            if operation is not None:
                delta = {"tool_calls": [_tool_call(call_index=index, name=operation[0], arguments=operation[1])]}
            interactions.append(build_cassette(
                asset_id=f"connection-wait-{index}", model="control-replay-model",
                url="https://opencode.ai/zen/v1/chat/completions",
                frames=chat_completion_frames(
                    response_id=f"chatcmpl-connection-wait-{index}", model="control-replay-model",
                    deltas=({"role": "assistant", **delta},), finish_reason="tool_calls" if operation else "stop",
                ),
            ).raw["interactions"][0])
        return replay.control.plan_model_calls(session, thread, interactions)
    return plan


async def task_resource(client, session, task_id, status):
    async with asyncio.timeout(30):
        while True:
            response = await client.get(f"/api/v1/sessions/{session}/resources")
            assert response.status_code == 200, response.text
            resources = response.json()["data"]["items"]
            item = next((item for item in resources if item["resource_id"] == task_id), None)
            if item is not None and item["status"] == status:
                return item
            assert item is None or item["status"] not in {"failed", "cancelled", "deleted"}, item
            await asyncio.sleep(0.05)


async def authorize(client, secure, operation_id):
    async with asyncio.timeout(12):
        while True:
            status = await client.get(f"/api/v1/mcp/connection-operations/{operation_id}", params={"wait_timeout_ms": 100})
            assert status.status_code == 200, status.text
            if status.json()["data"]["status"] == "waiting_user":
                break
    response = await client.get(f"/api/v1/mcp/connection-operations/{operation_id}/authorization")
    assert response.status_code == 200, response.text
    async with httpx.AsyncClient() as browser:
        redirect = await browser.get(response.json()["data"]["authorization_url"])
    assert redirect.status_code == 303
    callback = {name: values[0] for name, values in parse_qs(urlparse(redirect.headers["location"]).query).items()}
    response = await client.post(f"/api/v1/mcp/connection-operations/{operation_id}/callback", json=callback)
    assert response.status_code == 200, response.text
    duplicate = await client.post(f"/api/v1/mcp/connection-operations/{operation_id}/callback", json=callback)
    assert duplicate.status_code in {400, 409}
    return callback


@pytest.mark.parametrize(("boundary", "fault"), [
    ("turn", None), ("model_call", None),
    ("turn", "mcp.wait.mailbox_committed"), ("turn", "mcp.wait.dispatcher_accepted"),
])
async def test_authorization_continues_without_new_user_turn(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, wait_plan, wait_mount, boundary, fault,
):
    backends = []
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, boundary=boundary, startup_mount=wait_mount, backend_processes=backends) as (client, replay, case),
    ):
        session = await create_session(client, "持久授权等待")
        thread = (await client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]
        call_ids = wait_plan(replay, session, thread, "授权完成后检查 secure 的完整定义，再调用 probe 核实连接。")
        operation_id = software_tool_operation_id("mcp_connect", session, thread, str(UUID(call_ids[0])), "debug-e2e-call-0")
        task_id = mcp_wait_task_id(session, thread, operation_id)
        response = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "为 secure 发起连接并登记授权后继续，结束本次执行。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert response.status_code == 200, response.text
        accepted = response.json()["data"]
        await wait_for_job_done(client, accepted["job_id"], max_attempts=140)
        waiting = await task_resource(client, session, task_id, "running")
        assert waiting["metadata"]["thread_id"] == thread and "cancel" in waiting["available_actions"]
        original = json.loads((replay.artifacts / f"{call_ids[1]}.json").read_text())["request"]
        result = next(message for message in original["messages"] if message["role"] == "tool")
        assert result["tool_call_id"] == "debug-e2e-call-0"
        wait = json.loads(result["content"])["wait"]
        assert wait["task_id"] == task_id and wait["delivery_policy"] == "after_turn"
        if fault is not None:
            replay.control.arm(task_id, fault)
        callback = await authorize(client, secure, operation_id)
        if fault is not None:
            await asyncio.to_thread(replay.control.acknowledged, task_id, fault)
            if fault == "mcp.wait.dispatcher_accepted":
                # Job 已接受且完成，但后台投递尚未结算；恢复不能再次执行它。
                async with asyncio.timeout(45):
                    while not (replay.artifacts / f"{call_ids[-1]}.json").exists():
                        await asyncio.sleep(0.05)
                resolver = get_session_path_resolver(case.mount.workspace_root / ".boxteam/sessions")
                with closing(sqlite3.connect(resolver.resolve_thread_node(session, thread) / "rollout/index.sqlite")) as database:
                    internal = database.execute("SELECT job_id FROM executions WHERE execution_origin='internal'").fetchone()
                assert internal is not None
                await wait_for_job_done(client, internal[0], max_attempts=100)
            backends[0].process.kill()
            await asyncio.to_thread(backends[0].process.wait, 10)
            replay.control.release(task_id, fault)
            replacement = await asyncio.to_thread(
                start_backend_process, workspace_root=str(case.mount.workspace_root), port=backends[0].port,
                log_name="mcp-wait-restarted", asgi_app="tests.support.e2e_control.backend_app:app",
                startup_mount=wait_mount, env_overrides={
                    **replay.environment,
                    "BOXTEAM_HOME": str(case.context.boxteam_home_for_node(f"mcp-product-gateway:{case.mount.workspace_id}")),
                    "BOXTEAM_GATEWAY_URL": str(client.base_url).rstrip("/"),
                },
            )
            backends.append(replacement)
        completed = await task_resource(client, session, task_id, "completed")
        assert completed["metadata"]["result"]["delivery"] == "submitted"
        async with asyncio.timeout(45):
            while not (replay.artifacts / f"{call_ids[-1]}.json").exists():
                await asyncio.sleep(0.05)
        resolver = get_session_path_resolver(case.mount.workspace_root / ".boxteam/sessions")
        with closing(sqlite3.connect(resolver.resolve_thread_node(session, thread) / "rollout/index.sqlite")) as database:
            executions = database.execute("SELECT execution_id,execution_origin,turn_id,job_id FROM executions ORDER BY created_at").fetchall()
            assert len(executions) == 2 and executions[0][1:3] == ("user_turn", accepted["turn_id"])
            assert executions[1][1:3] == ("internal", None)
            assert database.execute("SELECT count(*) FROM turns").fetchone()[0] == 1
            assert database.execute("SELECT count(*) FROM runtime_execution_wakeups").fetchone()[0] == 1
        if fault != "mcp.wait.dispatcher_accepted":
            await wait_for_job_done(client, executions[1][3], max_attempts=100)
        records = [json.loads((replay.artifacts / f"{identity}.json").read_text())["request"] for identity in call_ids]
        saver = RolloutCheckpointSaver(resolver.sessions_root)
        assert saver.runtime_execution_outcome(session, thread, executions[1][0]) == "completed"
        assemblies = saver.list_context_assemblies(session, thread)
        assert len(assemblies) == len(records)
        for assembly, record in zip(assemblies, records, strict=True):
            wire = normalize_wire_request(record)
            assert assembly.request_hash_preimage == wire
            assert assembly.request_hash == context_request_hash(
                assembly.as_sealed_plan(), assembly.provider_version, projector_id=assembly.projector_id,
                projector_version=assembly.projector_version, target_format=assembly.target_format, wire_request=wire,
            )
        assert {assembly.execution_id for assembly in assemblies[:2]} == {executions[0][0]}
        assert {assembly.execution_id for assembly in assemblies[2:]} == {executions[1][0]}
        resumed = records[2]
        assert any("MCP 连接操作已核实成功" in str(message["content"]) and operation_id in str(message["content"]) for message in resumed["messages"])
        final_results = [message for message in records[-1]["messages"] if message["role"] == "tool"]
        found = next(json.loads(message["content"]) for message in final_results if message["tool_call_id"] == "debug-e2e-call-2")
        assert found["results"][0]["status"] == "available"
        invocation = next(message for message in final_results if message["tool_call_id"] == "debug-e2e-call-3")
        assert json.loads(invocation["content"][0]["text"])["arguments"] == {"proof": "authorized-continuation"}
        assert secure.exchanges == secure.registrations == 1
        raw = json.dumps(records)
        assert all(secret not in raw for secret in (secure.access_token, secure.refresh_token, callback["code"], callback["state"]))


async def test_cancelling_one_thread_wait_preserves_other_waiter_and_shared_connection(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, wait_plan, wait_mount,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, startup_mount=wait_mount) as (client, replay, case),
    ):
        waits = []
        operation_id = None
        for index in range(2):
            session = await create_session(client, f"独立等待 {index}")
            thread = (await client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]
            calls = wait_plan(replay, session, thread, "连接成功后核查并调用 probe", operation_id=operation_id)
            if operation_id is None:
                operation_id = software_tool_operation_id("mcp_connect", session, thread, str(UUID(calls[0])), "debug-e2e-call-0")
            task_id = mcp_wait_task_id(session, thread, operation_id)
            response = await client.post(f"/api/v1/sessions/{session}/messages", json={
                "message": {"content": "为 secure 登记等待，授权成功后继续。"},
                "run": {"mode": "single_agent", "agent_id": "default"},
            })
            assert response.status_code == 200, response.text
            await wait_for_job_done(client, response.json()["data"]["job_id"], max_attempts=140)
            await task_resource(client, session, task_id, "running")
            waits.append((session, thread, task_id, calls))
        cancelled_session, cancelled_thread, cancelled_task, cancelled_calls = waits[0]
        response = await client.post(
            f"/api/v1/sessions/{cancelled_session}/resources/background_task/{cancelled_task}/control", json={"action": "cancel"},
        )
        assert response.status_code == 200 and response.json()["data"]["status"] == "cancelled", response.text
        status = await client.get(f"/api/v1/mcp/connection-operations/{operation_id}")
        assert status.json()["data"]["status"] == "waiting_user" and secure.exchanges == 0
        await authorize(client, secure, operation_id)
        session, thread, task_id, calls = waits[1]
        completed = await task_resource(client, session, task_id, "completed")
        assert completed["metadata"]["result"]["delivery"] == "submitted"
        async with asyncio.timeout(45):
            while not (replay.artifacts / f"{calls[-1]}.json").exists():
                await asyncio.sleep(0.05)
        resolver = get_session_path_resolver(case.mount.workspace_root / ".boxteam/sessions")
        for selected_session, selected_thread, expected_count in [(cancelled_session, cancelled_thread, 1), (session, thread, 2)]:
            with closing(sqlite3.connect(resolver.resolve_thread_node(selected_session, selected_thread) / "rollout/index.sqlite")) as database:
                assert database.execute("SELECT count(*) FROM executions").fetchone()[0] == expected_count
                if selected_session == session:
                    internal = database.execute("SELECT job_id FROM executions WHERE execution_origin='internal'").fetchone()
        assert internal is not None
        await wait_for_job_done(client, internal[0], max_attempts=100)
        assert not (replay.artifacts / f"{cancelled_calls[2]}.json").exists()
        assert secure.exchanges == secure.registrations == 1
