"""真实三 Gateway 分页在连接中撤权、恢复后保持冻结历史与页身份。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path

import commentjson
import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.domain.itemized.identity.tool_call_identity import software_tool_operation_id
from app.gateway.federation.errors import FEDERATION_AUTHORIZATION_REVOKED
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationNode,
    FederatedCommunicationCase,
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_triad as communication_triad,  # noqa: PLC0414 — 复用正式三端进程 fixture
)
from tests.support.api_helpers import create_session


async def _message(node: CommunicationNode, session: str, content: str) -> dict[str, object]:
    response = await node.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": content}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    return response.json()["data"]


async def _policy_status(node: CommunicationNode, *, after: int = -1) -> dict[str, object]:
    async with asyncio.timeout(20):
        while True:
            response = await node.client.get("/api/gateway/config/reload-status")
            assert response.status_code == 200, response.text
            status = response.json()["data"]
            if status["state"] == "active" and status["candidate_ref"] is None and status["active_revision"] > after:
                assert status["healthy"] is True and status["restart_required"] is False, status
                return status
            await asyncio.sleep(0.05)


def _channels(node: CommunicationNode) -> list[tuple[object, ...]]:
    database = node.gateway_root / "federation/control.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        return connection.execute(
            "SELECT peer_gateway_id,channel_instance_id,channel_epoch,direction "
            "FROM federation_channel_registry ORDER BY peer_gateway_id",
        ).fetchall()


def _canonical_results(database: Path, call: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        return connection.execute(
            "SELECT item_id,status,execution_id FROM item_catalog "
            "WHERE semantic_kind='tool_result' AND tool_call_id=?", (call,),
        ).fetchall()


@pytest.mark.asyncio
async def test_read_cursor_rechecks_target_policy_and_keeps_original_snapshot(
    communication_triad: FederatedCommunicationCase,
) -> None:
    case = communication_triad
    source, target = case.nodes["b"], case.nodes["c"]
    source_session = await create_session(source.client, "分页撤权来源")
    target_session = await create_session(target.client, "分页撤权目标")
    await case.plan(target, target_session, [{"content": "policy-frozen-answer"}])
    target_dispatch = await _message(target, target_session, "policy-frozen-question")
    await _completed(target.client, target_dispatch["job_id"])
    arguments = {
        "resource_identity": str(session_resource_identity(target.workspace_id, target_session)),
        "uri": session_resource_uri(target.workspace_id, target_session, gateway_authority=target.gateway_id),
        "scope": "workspace", "view": "messages", "limit": 1,
    }
    first, = await case.plan(source, source_session, [
        _tool("invoke_extension_tool", {"tool_name": "read_context", "arguments": arguments}, "policy-page-0"),
    ])
    first_call = first + ":tool-call:policy-page-0"
    control = case.replay.control
    tool_phase, send_phase = "test.tool_result.committed", "observation.source.before_request"
    control.arm(first_call, tool_phase)
    second_operation = None
    assert target.gateway_home is not None
    config_path = target.gateway_home / "config/gateway.jsonc"
    original_config = config_path.read_text(encoding="utf-8")
    try:
        dispatch = await _message(source, source_session, "分页读取目标的冻结历史")
        await asyncio.to_thread(control.acknowledged, first_call, tool_phase, 60)
        first_result = json.loads(case.tool_result(source_session, first_call)["content"])
        assert first_result["next_cursor"].startswith("rc2.") and first_result["has_more"] is True
        assert len(first_result["items"]) == 1
        assert "policy-frozen-question" in json.dumps(first_result["items"])
        source_node = await source.session_node(source_session)
        target_node = await target.session_node(target_session)
        with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
            original, = store.list_remote_observations()
            first_page, = store.list_remote_pages(original.observation_id, side="source")
            assert first_page.result == first_result
        with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
            frozen = store.get_remote_observation(original.observation_id, side="target")
            target_page, = store.list_remote_pages(original.observation_id, side="target")
            target_lease = store.find_lease_by_operation("target-observation|" + original.observation_id)
            assert frozen.snapshot is not None

        await case.plan(target, target_session, [{"content": "policy-new-answer-must-not-leak"}])
        newer = await _message(target, target_session, "policy-new-question-must-not-leak")
        await _completed(target.client, newer["job_id"])
        second, final = await case.plan(source, source_session, [
            _tool("invoke_extension_tool", {"tool_name": "read_context", "arguments": {
                **arguments, "cursor": first_result["next_cursor"],
            }}, "policy-page-1"), {"content": "policy-source-completed"},
        ])
        thread = case.planned_calls[second]["thread_id"]
        second_call = second + ":tool-call:policy-page-1"
        second_operation = software_tool_operation_id("read", source_session, thread, second, "policy-page-1")
        control.arm(second_operation, send_phase)
        control.release(first_call, tool_phase)
        await asyncio.to_thread(control.acknowledged, second_operation, send_phase, 60)
        with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
            assert store.get_remote_observation(original.observation_id, side="source") == original
            page0, pending = store.list_remote_pages(original.observation_id, side="source")
            assert page0 == first_page and pending.page_ordinal == 1
            assert pending.result is pending.target_result is None
            envelope = pending.request["envelope"]
            assert envelope["source_call_id"] == second_operation
            assert envelope["page_identity"] == pending.page_identity
            assert envelope["query"]["read"]["cursor"] != first_result["next_cursor"]
        database = get_session_path_resolver(source.workspace / ".boxteam/sessions").resolve_thread_node(
            source_session, thread,
        ) / "rollout/index.sqlite"
        assert _canonical_results(database, second_call) == []
        channels_before = {name: _channels(node) for name, node in case.nodes.items()}
        before = await _policy_status(target)
        config = commentjson.loads(original_config)
        config.setdefault("permissions", {}).setdefault("federation", {})["rules"] = [
            {"capability": "read", "effect": "deny"},
        ]
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        revoked = await _policy_status(target, after=before["active_revision"])

        # 重试产品已冻结的原 envelope；不手工构造页身份，也不替产品推进账本。
        denied = await source.client.post("/api/gateway/federation/relay", json={
            "operation": "read", "target": envelope["target"], "body": envelope,
        })
        assert denied.status_code == 403, denied.text
        denial = denied.json()["detail"]
        assert denial["code"] == FEDERATION_AUTHORIZATION_REVOKED
        assert denial["detail"]["capability"] == "read"
        assert all(secret not in denied.text for secret in ("policy-frozen-question", "policy-frozen-answer", "policy-new-answer"))
        with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
            assert store.get_remote_observation(original.observation_id, side="target") == frozen
            assert store.list_remote_pages(original.observation_id, side="target") == (target_page,)
            assert store.find_lease_by_operation("target-observation|" + original.observation_id) == target_lease
        with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
            assert store.list_remote_pages(original.observation_id, side="source") == (first_page, pending)
        assert _canonical_results(database, second_call) == []
        assert not (case.replay.artifacts / (final + ".json")).exists()
        config_path.write_text(original_config, encoding="utf-8")
        restored = await _policy_status(target, after=revoked["active_revision"])
        control.release(second_operation, send_phase)
        job = await _completed(source.client, dispatch["job_id"])
    finally:
        config_path.write_text(original_config, encoding="utf-8")
        control.release(first_call, tool_phase)
        if second_operation is not None:
            control.release(second_operation, send_phase)

    second_result = json.loads(case.tool_result(source_session, second_call)["content"])
    assert second_result["revision"] == first_result["revision"]
    assert len(second_result["items"]) == 1
    assert "policy-frozen-answer" in json.dumps(second_result["items"])
    assert second_result["next_cursor"] is None and second_result["has_more"] is False
    assert "policy-new-" not in json.dumps(second_result)
    with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
        assert store.get_remote_observation(original.observation_id, side="source") == original
        recovered0, recovered1 = store.list_remote_pages(original.observation_id, side="source")
        assert recovered0 == first_page and recovered1.result == second_result
        assert recovered1.request == pending.request and recovered1.page_identity == pending.page_identity
        assert store.find_lease_by_operation("source-page|" + pending.page_identity).state == "completed"
    with closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as store:
        assert store.get_remote_observation(original.observation_id, side="target") == frozen
        target0, target1 = store.list_remote_pages(original.observation_id, side="target")
        assert target0 == target_page and target1.page_identity == pending.page_identity
        assert target1.page_ordinal == 1 and recovered1.target_result == target1.result
    results = [item for item in case.request(final)["messages"] if item["role"] == "tool"]
    assert [(item["tool_call_id"], json.loads(item["content"])) for item in results] == [
        ("policy-page-0", first_result), ("policy-page-1", second_result),
    ]
    requests = [case.request(call) for call in (first, second, final)]
    assert requests[0]["tools"] == requests[1]["tools"] == requests[2]["tools"]
    for previous, following in pairwise(requests):
        assert following["messages"][:len(previous["messages"])] == previous["messages"]
    canonical, = _canonical_results(database, second_call)
    assert canonical[1] == "completed"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT execution_id FROM executions WHERE job_id=?", (dispatch["job_id"],)).fetchall() == [(canonical[2],)]
    assert {name: _channels(node) for name, node in case.nodes.items()} == channels_before
    (case.artifacts / "read-policy-actual.json").write_text(json.dumps({
        "source": asdict(original), "frozen": asdict(frozen), "first_page": asdict(first_page),
        "pending_page": asdict(pending), "recovered_page": asdict(recovered1),
        "target_page": asdict(target1), "denial": denial,
        "policy_before": before, "policy_revoked": revoked, "policy_restored": restored,
        "channels": channels_before, "job": job, "canonical_result": canonical,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
