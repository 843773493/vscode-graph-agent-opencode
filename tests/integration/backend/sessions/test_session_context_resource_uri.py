"""A04 结构化资源 URI 的正式 Gateway/Workspace 星型链路验收。"""

import json
import sqlite3
from contextlib import closing

import pytest

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


@pytest.mark.asyncio
@pytest.mark.parametrize("communication_triad", [{"resource_service_nodes":("c",)}], indirect=True)
async def test_a04_remote_session_inventory_search_and_durable_read(communication_triad) -> None:
    nodes, model, artifacts = communication_triad
    source, target = nodes["b"], nodes["c"]
    target_session = await create_session(target.client, "a04-uri-target")
    source_session = await create_session(source.client, "a04-uri-source")
    session_uri = session_resource_uri(
        target.workspace_id, target_session, gateway_authority=target.gateway_id,
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
        assert response.json()["detail"]["code"] == code
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        evidence.append({"rejected":query, "code":code})

    tool_result = {}
    def reply(payload):
        tools = [message for message in payload["messages"] if message["role"] == "tool"]
        if not tools:
            return _tool("invoke_extension_tool", {"tool_name":"read_context", "arguments":{
                "resource_identity":str(refs[0][0]), "uri":session_uri, "scope":"workspace", "view":"information",
            }}, "call_a04_remote_read")
        tool_result.update(json.loads(tools[-1]["content"]))
        return {"content":"a04-uri-completed"}
    model.response_callback = reply
    response = await source.client.post(f"/api/v1/sessions/{source_session}/messages", json={
        "message":{"content":"a04-uri-source"}, "run":{"mode":"single_agent","agent_id":"default"},
    })
    assert response.status_code == 200, response.text
    await _completed(source.client, response.json()["data"]["job_id"])
    assert tool_result["uri"] == session_uri and tool_result["items"]
    source_node = await source.session_node(source_session)
    target_node = await target.session_node(target_session)
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
    with sqlite3.connect((target_node / "rollout/index.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
    serialized = json.dumps([evidence, tool_result], ensure_ascii=False)
    assert str(source.workspace) not in serialized and str(target.workspace) not in serialized
    assert "backend_url" not in serialized and "private_key" not in serialized
    (artifacts / "a04-resource-uri-core.json").write_text(serialized + "\n")
