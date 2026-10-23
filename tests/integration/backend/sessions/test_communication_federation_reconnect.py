"""真实联邦通道换代与源进程恢复保持已持久化的通信身份。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

import commentjson
import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.gateway.federation.errors import FEDERATION_AUTHORIZATION_REVOKED
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    FederatedCommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_triad as communication_triad,  # noqa: PLC0414 - 跨文件复用正式进程 fixture
)
from tests.support.api_helpers import create_session
from tests.support.communication_federation import CommunicationNode
from tests.support.e2e_control.harness import control_request


def _channel(node: CommunicationNode, peer_gateway_id: str) -> tuple[str, int] | None:
    database = node.gateway_root / "federation/control.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT channel_instance_id,channel_epoch FROM federation_channel_registry WHERE peer_gateway_id=?",
            (peer_gateway_id,),
        ).fetchall()
    assert len(rows) <= 1, rows
    return rows[0] if rows else None


@pytest.mark.asyncio
async def test_pinned_outbox_recovers_after_channel_epoch_without_discovery(
    communication_triad: FederatedCommunicationCase,
) -> None:
    case = communication_triad
    hub, source, target = (case.nodes[name] for name in ("a", "b", "c"))
    source_session = await create_session(source.client, "Reconnect source")
    target_session = await create_session(target.client, "Reconnect target")
    main = await source.client.get(f"/api/v1/sessions/{source_session}/main-thread")
    assert main.status_code == 200, main.text
    source_thread = main.json()["data"]["thread_id"]
    control = case.replay.control
    target_barrier = "reconnect-target-runtime"
    control.arm_match(target_barrier, "thread.runtime.acquired", {"session_id": target_session})
    target_call, = await case.plan(target, target_session, [{"content": "reconnect-target-completed"}])
    source_call, = await case.plan(source, source_session, [_tool("send_message_to_session", {
        "target_session_id": target_session, "content": "reconnect-pinned-message", "kind": "result",
    }, "call_reconnect_send")])
    operation_id = software_tool_operation_id(
        "send", source_session, source_thread, source_call, "call_reconnect_send",
    )
    phase = "communication.source.receipt_received"
    control.arm(operation_id, phase)
    accepted = await source.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "Send once, then recover the pinned outbox."},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert accepted.status_code == 200, accepted.text
    try:
        await asyncio.to_thread(control.acknowledged, operation_id, phase, 30)
        source_node = await source.session_node(source_session)
        target_node = await target.session_node(target_session)
        with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
            original = store.get_communication_outbox(operation_id)
            lease = store.find_lease_by_operation("communication-source|" + operation_id)
        assert original.state == "routing" and original.latest_receipt is None
        assert json.loads(original.target_ref) == [target_session, None]
        assert (original.target_gateway_id, original.target_workspace_id, original.target_session_id) == (
            target.gateway_id, target.workspace_id, target_session,
        )
        async with asyncio.timeout(30):
            while True:
                with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
                    inbox = store.get_communication_inbox(original.communication_id)
                if inbox.state == "execution_bound":
                    break
                assert inbox.state == "target_accepted", inbox
                await asyncio.sleep(0.05)

        # 已冻结目标后撤销 discovery；恢复若重新解析裸 ID，必然被真实 Gateway 拒绝。
        assert source.gateway_home is not None
        config_path = source.gateway_home / "config/gateway.jsonc"
        configuration = commentjson.loads(config_path.read_text(encoding="utf-8"))
        configuration.setdefault("permissions", {}).setdefault("federation", {})["rules"] = [
            {"capability": "discovery", "effect": "deny"},
        ]
        config_path.write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        discovery_body = {
            "source": {"gateway_id": source.gateway_id, "workspace_id": source.workspace_id,
                       "session_id": source_session, "thread_id": source_thread},
            "session_id": target_session,
        }
        async with asyncio.timeout(20):
            while True:
                denied = await source.client.post("/api/gateway/federation/discovery", json=discovery_body)
                if denied.status_code == 409:
                    break
                assert denied.status_code == 200, denied.text
                await asyncio.sleep(0.05)
        denial = denied.json()["detail"]
        assert denial["code"] == FEDERATION_AUTHORIZATION_REVOKED, denial
        assert denial["detail"]["capability"] == "discovery", denial

        before = _channel(hub, target.gateway_id)
        assert before is not None
        workspaces = await hub.client.get("/api/gateway/workspaces")
        assert workspaces.status_code == 200, workspaces.text
        remote = next(item for item in workspaces.json()["data"]["items"]
                      if item.get("remote") and item["remote"]["gateway_id"] == target.gateway_id)
        reconnected = await hub.client.post(f"/api/gateway/workspaces/{remote['workspace_id']}/reconnect")
        assert reconnected.status_code == 200, reconnected.text
        async with asyncio.timeout(20):
            while True:
                after = _channel(hub, target.gateway_id)
                if after is not None and after[0] != before[0]:
                    break
                await asyncio.sleep(0.05)
        assert after[1] > before[1], (before, after)
        killed = await asyncio.to_thread(source.restart_backend, "kill")
        control.release(operation_id, phase)
        restarted = await asyncio.to_thread(source.restart_backend, "start")

        # 只读账本观察 startup owner；不以 read/wait 工具替产品唤醒恢复。
        async with asyncio.timeout(30):
            while True:
                with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
                    recovered = store.get_communication_outbox(operation_id)
                    recovered_lease = store.find_lease_by_operation("communication-source|" + operation_id)
                if recovered.state == "target_accepted" and recovered_lease.state == "completed":
                    break
                await asyncio.sleep(0.05)
        assert asdict(recovered) == {**asdict(original), "state": "target_accepted",
                                    "latest_receipt": recovered.latest_receipt, "updated_at": recovered.updated_at}
        assert asdict(recovered_lease) == {**asdict(lease), "state": "completed",
                                          "revision": recovered_lease.revision,
                                          "updated_at": recovered_lease.updated_at}
        assert recovered_lease.revision > lease.revision
        receipt = json.loads(recovered.latest_receipt)
        assert receipt["target"] == {
            "gateway_id": original.target_gateway_id, "workspace_id": original.target_workspace_id,
            "session_id": original.target_session_id, "thread_id": original.target_thread_id,
        }
        assert receipt["admission_id"] == inbox.admission_id
        with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
            assert store.get_communication_inbox(original.communication_id) == inbox
        for node, table in ((source_node, "communication_outbox"), (target_node, "communication_inbox")):
            with sqlite3.connect((node / CONTROL_DATABASE_NAME).as_uri() + "?mode=ro", uri=True) as connection:
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        still_denied = await source.client.post("/api/gateway/federation/discovery", json=discovery_body)
        assert still_denied.status_code == 409 and still_denied.json()["detail"] == denial
        (case.artifacts / "reconnect-actual.json").write_text(json.dumps({
            "outbox_before": asdict(original), "outbox_after": asdict(recovered), "inbox": asdict(inbox),
            "lease_before": asdict(lease), "lease_after": asdict(recovered_lease),
            "channel_before": before, "channel_after": after, "discovery_denial": denial,
            "source_killed": killed, "source_restarted": restarted,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        control.release(operation_id, phase)
        control_request(json.loads(control.environment["BOXTEAM_E2E_TEST_CONTROL"]), {
            "action": "release_match", "barrier_id": target_barrier,
        })
    await _completed(target.client, inbox.job_id)
    assert "reconnect-pinned-message" in json.dumps(case.request(target_call)["messages"], ensure_ascii=False)
    database = get_session_path_resolver(target.workspace / ".boxteam/sessions").resolve_thread_node(
        target_session, inbox.target_thread_id,
    ) / "rollout/index.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT execution_id,outcome FROM executions").fetchall() == [
            (inbox.execution_id, "completed"),
        ]
        assert connection.execute("SELECT source_id,job_id FROM runtime_execution_wakeups").fetchall() == [
            (inbox.communication_id, inbox.job_id),
        ]
