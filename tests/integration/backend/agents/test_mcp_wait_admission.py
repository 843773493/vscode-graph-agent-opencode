"""从真实授权等待验证用户输入、删除 fence 和原期限，关键状态均经产品入口生成。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.abstractions.mcp_connection_wait import mcp_wait_task_id
from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.agents.test_mcp_connection_wait import (
    authorize,
    task_resource,
)
from tests.integration.backend.agents.test_mcp_connection_wait import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414
)
from tests.integration.backend.agents.test_mcp_connection_wait import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414
)
from tests.integration.backend.agents.test_mcp_connection_wait import (
    product_case as product_case,  # noqa: PLC0414
)
from tests.integration.backend.agents.test_mcp_connection_wait import (
    recovery_case as recovery_case,  # noqa: PLC0414
)
from tests.integration.backend.agents.test_mcp_connection_wait import (
    wait_mount as wait_mount,  # noqa: PLC0414
)
from tests.integration.backend.agents.test_mcp_connection_wait import (
    wait_plan as wait_plan,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_mcp_config_recovery import RecoveryCase
from tests.integration.workspace_services.mcp.oauth_server import OAuthEndpoint
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.model_stream.config import ModelStreamReplay


@dataclass(frozen=True)
class PendingWait:
    client: httpx.AsyncClient
    replay: ModelStreamReplay
    case: RecoveryCase
    secure: OAuthEndpoint
    session: str
    thread: str
    operation: str
    task: str
    calls: list[str]
    turn: str
    deadline: float
    index: Path


@pytest.fixture
async def pending_wait(request, product_case, oauth_endpoint_factory, http_schema_endpoint_factory, wait_plan, wait_mount):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, startup_mount=wait_mount) as (client, replay, case),
    ):
        session = await create_session(client, "授权等待准入边界")
        thread = (await client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]
        calls = wait_plan(replay, session, thread, "连接成功后检查定义并调用 probe。", interleaved_user=getattr(request, "param", False))
        operation = software_tool_operation_id("mcp_connect", session, thread, str(UUID(calls[0])), "debug-e2e-call-0")
        task = mcp_wait_task_id(session, thread, operation)
        response = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "为 secure 发起连接，登记成功后自动继续，并结束当前执行。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert response.status_code == 200, response.text
        accepted = response.json()["data"]
        await wait_for_job_done(client, accepted["job_id"], max_attempts=140)
        await task_resource(client, session, task, "running")
        operation_response = await client.get(f"/api/v1/mcp/connection-operations/{operation}")
        assert operation_response.status_code == 200, operation_response.text
        stored = operation_response.json()["data"]
        assert stored["status"] == "waiting_user"
        resolver = get_session_path_resolver(case.mount.workspace_root / ".boxteam/sessions")
        yield PendingWait(client, replay, case, secure, session, thread, operation, task, calls, accepted["turn_id"],
                          stored["deadline"], resolver.resolve_thread_node(session, thread) / "rollout/index.sqlite")


def verify_requests(wait: PendingWait, expected_count: int) -> list[dict]:
    records = [json.loads((wait.replay.artifacts / f"{identity}.json").read_text())["request"] for identity in wait.calls[:expected_count]]
    resolver = get_session_path_resolver(wait.case.mount.workspace_root / ".boxteam/sessions")
    with RolloutCheckpointSaver(resolver.sessions_root) as saver:
        assemblies = saver.list_context_assemblies(wait.session, wait.thread)
    assert len(assemblies) == expected_count
    for assembly, record in zip(assemblies, records, strict=True):
        assert (assembly.session_id, assembly.thread_id) == (wait.session, wait.thread)
        wire = normalize_wire_request(record)
        assert assembly.request_hash_preimage == wire
        assert assembly.request_hash == context_request_hash(
            assembly.as_sealed_plan(), assembly.provider_version, projector_id=assembly.projector_id,
            projector_version=assembly.projector_version, target_format=assembly.target_format, wire_request=wire,
        )
    return records


@pytest.mark.parametrize("pending_wait", [True], indirect=True)
async def test_user_input_finishes_before_authorization_continuation(pending_wait: PendingWait):
    wait = pending_wait
    wait.replay.control.arm(wait.calls[2], "model.response_ready")
    response = await wait.client.post(f"/api/v1/sessions/{wait.session}/messages", json={
        "message": {"content": "授权期间的新输入：先完成这条用户消息。"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    user = response.json()["data"]
    await asyncio.to_thread(wait.replay.control.acknowledged, wait.calls[2], "model.response_ready")
    try:
        await authorize(wait.client, wait.secure, wait.operation)
        completed = await task_resource(wait.client, wait.session, wait.task, "completed")
        assert completed["metadata"]["result"]["delivery"] == "submitted"
        # 唤醒已经持久接受，但正在生成的用户回复仍被真实模型屏障阻挡。
        assert not (wait.replay.artifacts / f"{wait.calls[3]}.json").exists()
        with closing(sqlite3.connect(wait.index)) as database:
            assert database.execute("SELECT count(*) FROM runtime_execution_wakeups").fetchone()[0] == 1
        assert not wait.secure.receipts.exists() or not wait.secure.receipts.read_text().strip()
    finally:
        wait.replay.control.release(wait.calls[2], "model.response_ready")
    await wait_for_job_done(wait.client, user["job_id"], max_attempts=100)
    async with asyncio.timeout(45):
        while not (wait.replay.artifacts / f"{wait.calls[-1]}.json").exists():
            await asyncio.sleep(0.05)
    with closing(sqlite3.connect(wait.index)) as database:
        executions = database.execute(
            "SELECT execution_origin,turn_id,job_id FROM executions ORDER BY created_at",
        ).fetchall()
        assert [row[:2] for row in executions] == [("user_turn", wait.turn), ("user_turn", user["turn_id"]), ("internal", None)]
        assert database.execute("SELECT count(*) FROM turns").fetchone()[0] == 2
    await wait_for_job_done(wait.client, executions[-1][2], max_attempts=100)
    records = verify_requests(wait, 6)
    assert any("授权期间的新输入" in str(message["content"]) for message in records[2]["messages"])
    assert not any("MCP 连接操作已核实成功" in str(message["content"]) for message in records[2]["messages"])
    assert any("MCP 连接操作已核实成功" in str(message["content"]) for message in records[3]["messages"])
    results = [message for message in records[-1]["messages"] if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in results] == ["debug-e2e-call-0", "debug-e2e-call-3", "debug-e2e-call-4"]
    assert json.loads(results[-2]["content"])["results"][0]["status"] == "available"
    assert json.loads(results[-1]["content"][0]["text"])["arguments"] == {"proof": "authorized-continuation"}
    assert wait.secure.exchanges == wait.secure.registrations == 1


async def delete_session(client: httpx.AsyncClient, session: str) -> dict:
    catalog = await client.get("/api/v1/session-catalog/snapshot")
    assert catalog.status_code == 200, catalog.text
    operation = create_prefixed_id("op")
    response = await client.post("/api/v1/session-catalog/operations:enqueue", json={"intents": [{
        "client_operation_id": operation, "client_sequence": 1, "kind": "delete_session",
        "target_node_id": session, "base_catalog_revision": catalog.json()["data"]["catalog_revision"],
    }]})
    assert response.status_code == 202, response.text
    async with asyncio.timeout(30):
        while True:
            response = await client.get("/api/v1/session-catalog/operations", params={"operation_id": operation})
            assert response.status_code == 200, response.text
            receipt = response.json()["data"]["items"][0]
            if receipt["state"] == "rejected":
                return receipt
            assert receipt["state"] not in {"cancelled", "dependency_failed"}, receipt
            if receipt["state"] == "committed" and receipt["settlement"] == "settled":
                return receipt
            await asyncio.sleep(0.05)


async def ready_operation(client: httpx.AsyncClient, operation: str) -> dict:
    async with asyncio.timeout(12):
        while True:
            response = await client.get(f"/api/v1/mcp/connection-operations/{operation}", params={"wait_timeout_ms": 500})
            assert response.status_code == 200, response.text
            result = response.json()["data"]
            if result["status"] == "succeeded":
                return result
            assert result["status"] not in {"failed", "expired", "cancelled"}, result


async def test_deleted_session_cannot_resume_but_shared_authorization_can_complete(pending_wait: PendingWait):
    wait = pending_wait
    verify_requests(wait, 2)
    blocked = await delete_session(wait.client, wait.session)
    assert blocked["state"] == "rejected" and blocked["error_code"] == "conflict"
    assert wait.task in blocked["error_detail"]
    # 保持原产品合同：活动后台任务阻挡删除，先由用户明确取消，再提交新的删除。
    response = await wait.client.post(
        f"/api/v1/sessions/{wait.session}/resources/background_task/{wait.task}/control", json={"action": "cancel"},
    )
    assert response.status_code == 200 and response.json()["data"]["status"] == "cancelled", response.text
    deleted = await delete_session(wait.client, wait.session)
    assert deleted["state"] == "committed" and deleted["settlement"] == "settled", deleted
    await authorize(wait.client, wait.secure, wait.operation)
    await ready_operation(wait.client, wait.operation)
    assert (await wait.client.get(f"/api/v1/sessions/{wait.session}")).status_code == 404
    assert not wait.index.exists()
    assert all(not (wait.replay.artifacts / f"{identity}.json").exists() for identity in wait.calls[2:])
    assert not wait.secure.receipts.exists() or not wait.secure.receipts.read_text().strip()
    assert wait.secure.exchanges == wait.secure.registrations == 1


async def test_wait_registered_after_authorization_observes_original_terminal_state(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, wait_plan, wait_mount,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, startup_mount=wait_mount) as (client, replay, case),
    ):
        operation = "authorized-before-wait"
        response = await client.post("/api/v1/mcp/connection-operations", json={
            "operation_id": operation, "server_id": "secure", "action": "connect",
        })
        assert response.status_code == 200, response.text
        await authorize(client, secure, operation)
        ready = await ready_operation(client, operation)
        deadline = ready["deadline"]
        session = await create_session(client, "已授权后再登记等待")
        thread = (await client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]
        calls = wait_plan(replay, session, thread, "连接已经成功，读取完整定义并核实 probe。", operation_id=operation)
        task = mcp_wait_task_id(session, thread, operation)
        response = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "查询已完成的连接并明确登记继续意图，结束本次执行。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert response.status_code == 200, response.text
        accepted = response.json()["data"]
        await wait_for_job_done(client, accepted["job_id"], max_attempts=100)
        completed = await task_resource(client, session, task, "completed")
        assert completed["metadata"]["result"]["delivery"] == "submitted"
        async with asyncio.timeout(45):
            while not (replay.artifacts / f"{calls[-1]}.json").exists():
                await asyncio.sleep(0.05)
        resolver = get_session_path_resolver(case.mount.workspace_root / ".boxteam/sessions")
        index = resolver.resolve_thread_node(session, thread) / "rollout/index.sqlite"
        with closing(sqlite3.connect(index)) as database:
            executions = database.execute("SELECT execution_origin,turn_id,job_id FROM executions ORDER BY created_at").fetchall()
            assert [row[:2] for row in executions] == [("user_turn", accepted["turn_id"]), ("internal", None)]
            assert database.execute("SELECT count(*) FROM runtime_execution_wakeups").fetchone()[0] == 1
        await wait_for_job_done(client, executions[-1][2], max_attempts=100)
        records = verify_requests(PendingWait(client, replay, case, secure, session, thread, operation, task, calls,
                                             accepted["turn_id"], deadline, index), 5)
        results = [message for message in records[-1]["messages"] if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in results] == ["debug-e2e-call-0", "debug-e2e-call-2", "debug-e2e-call-3"]
        assert json.loads(results[0]["content"])["operation"]["status"] == "succeeded"
        assert json.loads(results[-1]["content"][0]["text"])["arguments"] == {"proof": "authorized-continuation"}
        assert secure.exchanges == secure.registrations == 1


async def test_successful_authorization_does_not_renew_wait_deadline(pending_wait: PendingWait):
    wait = pending_wait
    wait.replay.control.arm(wait.task, "mcp.wait.mailbox_committed")
    try:
        await authorize(wait.client, wait.secure, wait.operation)
        await asyncio.to_thread(wait.replay.control.acknowledged, wait.task, "mcp.wait.mailbox_committed")
        mailbox_committed_at = time.time()
        remaining = wait.deadline - time.time()
        assert 0 < remaining < 300
        # 使用产品原有300秒期限，不改时钟、不改持久记录，也不缩短被验收的期限。
        await asyncio.sleep(remaining + 0.05)
    finally:
        wait.replay.control.release(wait.task, "mcp.wait.mailbox_committed")
    completed = await task_resource(wait.client, wait.session, wait.task, "completed")
    assert completed["metadata"]["result"]["delivery"] == "deadline_exceeded"
    operation = await ready_operation(wait.client, wait.operation)
    assert operation["deadline"] == wait.deadline
    servers = await wait.client.get("/api/v1/mcp/servers")
    assert servers.status_code == 200, servers.text
    assert next(server for server in servers.json()["data"] if server["server_id"] == "secure")["status"] == "ready"
    verify_requests(wait, 2)
    with closing(sqlite3.connect(wait.index)) as database:
        assert database.execute("SELECT count(*) FROM executions").fetchone()[0] == 1
        assert database.execute("SELECT count(*) FROM runtime_execution_wakeups").fetchone()[0] == 0
    assert all(not (wait.replay.artifacts / f"{identity}.json").exists() for identity in wait.calls[2:])
    assert not wait.secure.receipts.exists() or not wait.secure.receipts.read_text().strip()
    assert wait.secure.exchanges == wait.secure.registrations == 1
    (wait.case.context.artifacts_dir / "wait-deadline.json").write_text(json.dumps({
        "operation_id": wait.operation, "task_id": wait.task, "deadline": wait.deadline,
        "mailbox_committed_at": mailbox_committed_at, "settled_at": time.time(),
        "delivery": completed["metadata"]["result"]["delivery"], "connection_status": "ready",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
