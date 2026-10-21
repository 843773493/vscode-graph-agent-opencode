"""A04 结构化资源 URI 的正式 Gateway/Workspace 星型链路验收。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

import pytest

from app.core.path_utils import get_session_path_resolver, workspace_context
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.services.business.session_context_resource import (
    gateway_session_inventory_identity,
    gateway_session_inventory_uri,
    session_resource_identity,
    session_resource_uri,
    workspace_session_inventory_identity,
    workspace_session_inventory_uri,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_triad as communication_triad,  # noqa: PLC0414 - 正式 fixture 按当前测试路径隔离
)
from tests.support.api_helpers import create_session

AMBIGUOUS_SESSION = "ses_019c390d6a707c858709afddc785bf93"


def cold_materialization_counts(thread_node):
    """只读核验所有物化 owner，不能只用 executions=0 代替 cold 合同。"""
    database = thread_node / "rollout/index.sqlite"
    tables = ("executions", "model_calls", "context_assemblies", "source_overlays",
              "context_contributions", "context_source_control_states")
    if not database.exists():
        return {table: 0 for table in tables}
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(tables) <= existing, f"cold owner 数据库缺少表: {sorted(set(tables) - existing)}"
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}


@pytest.mark.asyncio
@pytest.mark.parametrize("communication_triad", [{"resource_service_nodes":("c",)}], indirect=True)
async def test_a04_remote_session_inventory_search_and_durable_read(communication_triad) -> None:
    case = communication_triad
    nodes, artifacts = case.nodes, case.artifacts
    source, target = nodes["b"], nodes["c"]
    target_session = await create_session(target.client, "a04-uri-target")
    source_session = await create_session(source.client, "a04-uri-source")
    session_uri = session_resource_uri(
        target.workspace_id, target_session, gateway_authority=target.gateway_id,
    )
    assert workspace_session_inventory_uri(
        target.workspace_id, gateway_authority=target.gateway_id,
    ) == (
        f"boxteam://{target.gateway_id}/workspace/{target.workspace_id}/resources/session"
    )
    refs = (
        (session_resource_identity(target.workspace_id, target_session), session_uri, "workspace", "information"),
        (workspace_session_inventory_identity(target.workspace_id), workspace_session_inventory_uri(
            target.workspace_id, gateway_authority=target.gateway_id,
        ), "workspace", "inventory"),
        (gateway_session_inventory_identity(target.gateway_id), gateway_session_inventory_uri(
            target.gateway_id, gateway_authority=target.gateway_id,
        ), "gateway", "inventory"),
    )
    evidence = []
    for identity, uri, scope, view in refs:
        request = {"resource_identity":str(identity), "uri":uri, "scope":scope, "view":view}
        response = await source.client.post("/api/v1/context/read", json=request)
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        result = response.json()["data"]
        assert (result["resource_identity"], result["uri"], result["scope"], result["view"]) == (
            str(identity), uri, scope, view,
        )
        assert result["items"] and result["returned_chars"] <= 16384
        evidence.append({"read":request, "result":result})
        search = {key:request[key] for key in ("resource_identity","uri","scope")}
        for source_name in ("session_catalog", "session_information"):
            search.update({"query":"a04-uri-target", "sources":[source_name]})
            found = await source.client.post("/api/v1/context/search", json=search)
            assert found.status_code == 200, found.text
            assert found.headers["X-Request-ID"] == found.json()["request_id"]
            matches = found.json()["data"]
            assert matches["uri"] == uri and matches["matches"]
            assert all(match["uri"] == session_uri for match in matches["matches"])
            assert not matches["partial_errors"]
            evidence.append({"search":dict(search), "result":matches})

    missing_session = "ses_019c390d6a707c858709afddc785bf93"
    missing_workspace = "33333333-3333-4333-8333-333333333333"
    rejected = (
        (refs[0][0], session_uri, "inventory", "unsupported_view"),
        (session_resource_identity(target.workspace_id, missing_session), session_resource_uri(
            target.workspace_id, missing_session, gateway_authority=target.gateway_id,
        ), "information", "target_not_resolvable"),
        (workspace_session_inventory_identity(missing_workspace), workspace_session_inventory_uri(
            missing_workspace, gateway_authority=target.gateway_id,
        ), "inventory", "target_not_resolvable"),
        (session_resource_identity(target.workspace_id, target_session), session_resource_uri(
            target.workspace_id, target_session, gateway_authority="gateway_unknown",
        ), "information", "federation-unknown-peer"),
    )
    for identity, uri, view, code in rejected:
        query = {"resource_identity":str(identity), "uri":uri, "scope":"workspace", "view":view}
        response = await source.client.post("/api/v1/context/read", json=query)
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        if code == "unsupported_view":
            assert detail["code"] == "federation-target-owner-rejected"
            assert detail["detail"] == {"owner": "resource_uri_resolver", "code": code}
        else:
            assert detail["code"] == code
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        evidence.append({"rejected":query, "code":code})

    calls = await case.plan(source, source_session, [
        _tool("invoke_extension_tool", {"tool_name": "read_context", "arguments": {
            "resource_identity": str(refs[0][0]), "uri": session_uri, "scope": "workspace", "view": "information",
        }}, "call_a04_remote_read"), {"content": "a04-uri-completed"},
    ])
    response = await source.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message":{"content":"a04-uri-source"}, "run":{"mode":"single_agent","agent_id":"default"},
    })
    assert response.status_code == 200, response.text
    await _completed(source.client, response.json()["data"]["job_id"])
    tool_message, = [message for message in case.request(calls[-1])["messages"] if message["role"] == "tool"]
    tool_result = json.loads(tool_message["content"])
    assert tool_result["uri"] == session_uri and tool_result["items"]
    source_node = await source.session_node(source_session)
    with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as store:
        observations = [item for item in store.list_remote_observations() if item.side == "source"]
        assert len(observations) == 1
        stored = observations[0].request
        frozen = stored["envelope"]
        target_main = await target.client.get(f"/api/v1/sessions/{target_session}/main-thread")
        assert target_main.status_code == 200, target_main.text
        assert frozen["target"] == {
            "gateway_id": target.gateway_id,
            "workspace_id": target.workspace_id,
            "session_id": target_session,
            "thread_id": target_main.json()["data"]["thread_id"],
        }
        assert frozen["operation"] == "read"
        assert stored["input"]["target_session_id"] == session_uri
        assert stored["input"]["target_workspace_id"] == target.workspace_id
        read = frozen["query"]["read"]
        assert {key: read[key] for key in ("resource_identity", "uri", "scope", "view")} == {
            "resource_identity": str(refs[0][0]), "uri": session_uri,
            "scope": "workspace", "view": "information",
        }
        assert read["cursor"] is None
        evidence.append({"durable_read": stored})
    resolver = get_session_path_resolver(target.workspace / ".boxteam/sessions")
    target_thread_node = resolver.resolve_thread_node(target_session, resolver.main_thread_id(target_session))
    assert all(count == 0 for count in cold_materialization_counts(target_thread_node).values())
    serialized = json.dumps([evidence, tool_result], ensure_ascii=False)
    assert str(source.workspace) not in serialized and str(target.workspace) not in serialized
    assert "backend_url" not in serialized and "private_key" not in serialized
    (artifacts / "a04-resource-uri-core.json").write_text(serialized + "\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("communication_triad", [{"resource_service_nodes": ("c",),
    "shared_session_ids": (AMBIGUOUS_SESSION,)}], indirect=True)
async def test_a06_bare_and_uri_send_wait_read_share_frozen_target_across_activation_reconnect(communication_triad):
    case = communication_triad
    nodes, artifacts = case.nodes, case.artifacts
    source, target, hub = nodes["b"], nodes["c"], nodes["a"]
    # 全部业务请求固定 Gateway workspace ID，不能依赖 active target。
    inventories = {}
    for name, node in nodes.items():
        response = await node.client.get("/api/gateway/workspaces")
        assert response.status_code == 200, response.text
        inventories[name] = response.json()["data"]
        own = next(item for item in inventories[name]["items"] if item["backend_workspace_id"] == node.workspace_id
                   and item["connection_kind"] == "local")
        node.client.headers["X-BoxTeam-Workspace-Id"] = own["workspace_id"]
    source_session = await create_session(source.client, "a06-uri-source")
    target_session = await create_session(target.client, "a06-uri-target")
    cold_session = await create_session(target.client, "a06-uri-cold")
    source_main = (await source.client.get(f"/api/v1/sessions/{source_session}/main-thread")).json()["data"]["thread_id"]
    target_main = (await target.client.get(f"/api/v1/sessions/{target_session}/main-thread")).json()["data"]["thread_id"]
    address = {"gateway_id": target.gateway_id, "workspace_id": target.workspace_id,
               "session_id": target_session, "thread_id": target_main}
    source_address = {"gateway_id": source.gateway_id, "workspace_id": source.workspace_id,
                      "session_id": source_session, "thread_id": source_main}
    uri = session_resource_uri(target.workspace_id, target_session, gateway_authority=target.gateway_id)
    cold_uri = session_resource_uri(target.workspace_id, cold_session, gateway_authority=target.gateway_id)
    cold_main = (await target.client.get(f"/api/v1/sessions/{cold_session}/main-thread")).json()["data"]["thread_id"]
    with workspace_context(target.workspace_id, target.workspace):
        resolver = get_session_path_resolver(target.workspace / ".boxteam/sessions")
        cold_thread_node = resolver.resolve_thread_node(cold_session, cold_main)
        assert cold_thread_node == resolver.resolve_session_node(cold_session) / "threads" / cold_main
    assert cold_thread_node.is_dir()
    before = cold_materialization_counts(cold_thread_node)
    assert not any(before.values())
    read_request = {"resource_identity": str(session_resource_identity(target.workspace_id, cold_session)),
                    "uri": cold_uri, "scope": "workspace", "view": "information"}
    read = await source.client.post("/api/v1/context/read", json=read_request)
    assert read.status_code == 200, read.text
    assert read.headers["X-Request-ID"] == read.json()["request_id"]
    assert read.json()["data"]["uri"] == cold_uri and read.json()["data"]["items"]
    assert cold_materialization_counts(cold_thread_node) == before
    rejected = await source.client.post("/api/v1/context/read", json={**read_request, "scope": "gateway"})
    assert rejected.status_code == 400 and rejected.json()["detail"]["code"] == "scope_mismatch", rejected.text
    ambiguity = await source.client.post("/api/gateway/federation/discovery", json={
        "source": source_address, "session_id": AMBIGUOUS_SESSION,
    })
    assert ambiguity.status_code == 409 and ambiguity.json()["detail"]["code"] == "target_ambiguous", ambiguity.text
    unreachable = await source.client.post("/api/gateway/federation/discovery", json={
        "source": source_address, "session_id": "ses_019c390d6a707c858709afddc785bf94", "gateway_id": "gateway_unknown",
    })
    assert unreachable.status_code == 409, unreachable.text
    assert unreachable.json()["detail"]["code"] in {"federation-unknown-peer", "target_not_resolvable"}
    control = case.replay.control
    await case.plan(target, target_session, [{"content": "a06-target-completed"}, {"content": "a06-target-completed"}])
    first_call, = await case.plan(source, source_session, [
        _tool("send_message_to_session", {"target_session_id": target_session,
              "content": "a06-message-0", "kind": "result"}, "call_a06_send_0"),
    ])
    send_call = first_call + ":tool-call:call_a06_send_0"
    control.arm(send_call, "test.tool_result.committed")
    response = await source.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message": {"content": "a06-joint-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    try:
        await asyncio.to_thread(control.acknowledged, send_call, "test.tool_result.committed", 40)
        accepted_event, = [event for event in control.events if event["operation_id"] == send_call
                           and event["phase"] == "test.tool_result.committed"]
        # 与原模型屏障一致：durable accepted后只允许45秒完成activate/reconnect并释放。
        async with asyncio.timeout_at(accepted_event["at"] + 45):
            receipt = json.loads(case.tool_result(source_session, send_call)["content"])
            remote_b = next(item for item in inventories["a"]["items"]
                            if (item.get("remote") or {}).get("gateway_id") == source.gateway_id)
            remote_c = next(item for item in inventories["a"]["items"]
                            if (item.get("remote") or {}).get("gateway_id") == target.gateway_id)
            activation = await hub.client.post(f"/api/gateway/workspaces/{remote_b['workspace_id']}/activate")
            assert activation.status_code == 200, activation.text
            reconnection = await hub.client.post(f"/api/gateway/workspaces/{remote_c['workspace_id']}/reconnect")
            assert reconnection.status_code == 200, reconnection.text
            middle = await case.plan(source, source_session, [
                _tool("wait_for_session", {"target_session_id": target_session,
                      "communication_id": receipt["communication_id"], "timeout_seconds": 30}, "call_a06_wait_0"),
                _tool("send_message_to_session", {"target_session_id": uri,
                      "content": "a06-message-1", "kind": "result"}, "call_a06_send_1"),
            ])
            second_send = middle[-1] + ":tool-call:call_a06_send_1"
            control.arm(second_send, "test.tool_result.committed")
    finally:
        control.release(send_call, "test.tool_result.committed")
    async with asyncio.timeout(60):
        try:
            await asyncio.to_thread(control.acknowledged, second_send, "test.tool_result.committed", 30)
            second_receipt = json.loads(case.tool_result(source_session, second_send)["content"])
            following = await case.plan(source, source_session, [
                _tool("wait_for_session", {"target_session_id": uri,
                      "communication_id": second_receipt["communication_id"], "timeout_seconds": 30}, "call_a06_wait_1"),
                _tool("invoke_extension_tool", {"tool_name": "read_context", "arguments": {
                    "resource_identity": str(session_resource_identity(target.workspace_id, target_session)),
                    "uri": uri, "scope": "workspace", "view": "messages"}}, "call_a06_read"),
                {"content": "a06-source-completed"},
            ])
        finally:
            control.release(second_send, "test.tool_result.committed")
        await _completed(source.client, response.json()["data"]["job_id"])
    tools = [message for message in case.request(following[-1])["messages"] if message["role"] == "tool"]
    assert len(tools) == 5
    keys = ("call_a06_send_0", "call_a06_wait_0", "call_a06_send_1", "call_a06_wait_1", "call_a06_read")
    result_calls = (first_call, middle[0], middle[1], following[0], following[1])
    assert [message["tool_call_id"] for message in tools] == list(keys)
    for call, key, message in zip(result_calls, keys, tools, strict=True):
        committed = case.tool_result(source_session, call + ":tool-call:" + key)
        assert json.loads(committed["content"]) == json.loads(message["content"])
    results = {key: json.loads(message["content"]) for key, message in zip(keys, tools, strict=True)}
    for index in range(2):
        assert results[f"call_a06_send_{index}"]["target"] == address
        assert results[f"call_a06_wait_{index}"]["target"] == address
        assert results[f"call_a06_wait_{index}"]["status"] == "completed"
    assert results["call_a06_read"]["uri"] == uri and results["call_a06_read"]["items"]
    source_node, target_node = await source.session_node(source_session), await target.session_node(target_session)
    with closing(SessionControlStore(source_node / CONTROL_DATABASE_NAME)) as outbox_owner, closing(SessionControlStore(target_node / CONTROL_DATABASE_NAME)) as inbox_owner:
        observations = [value for value in outbox_owner.list_remote_observations() if value.side == "source"]
        assert len(observations) == 3
        assert all(value.request["envelope"]["target"] == address for value in observations)
        inboxes = [inbox_owner.get_communication_inbox(results[f"call_a06_send_{index}"]["communication_id"]) for index in range(2)]
        for inbox in inboxes:
            assert (inbox.target_gateway_id, inbox.target_workspace_id, inbox.session_id, inbox.target_thread_id) == (
                target.gateway_id, target.workspace_id, target_session, target_main)
            assert inbox.state == "execution_bound"
        evidence = {"address": address, "tools": results, "inboxes": [asdict(value) for value in inboxes],
                    "frozen_observations": [value.request for value in observations], "cold_before": before,
                    "cold_after": cold_materialization_counts(cold_thread_node),
                    "ambiguity": ambiguity.json(), "unreachable": unreachable.json()}
    assert evidence["cold_after"] == before
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2)
    assert str(source.workspace) not in serialized and str(target.workspace) not in serialized
    assert "backend_url" not in serialized and "private_key" not in serialized
    (artifacts / "a06-uri-send-wait-read-joint.json").write_text(serialized + "\n")
