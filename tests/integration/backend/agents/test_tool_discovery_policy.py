"""真实产品入口核对初始禁用目录的身份、隐藏与执行拒绝。"""

from __future__ import annotations

import asyncio
import json
import secrets

import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.integration.backend.agents.test_debug_prompt_flow import _tool_call
from tests.integration.backend.agents.test_mcp_connection_control import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 同一真实 MCP Server
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414 - 同一真实 OAuth Server
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    product_case as product_case,  # noqa: PLC0414 - 同一 Gateway／Backend 装具
)
from tests.integration.backend.agents.test_mcp_connection_control import (
    recovery_case as recovery_case,  # noqa: PLC0414 - 本文件正式工作区
)
from tests.integration.backend.agents.test_mcp_connection_wait import authorize
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done


@pytest.fixture
def discovery_plan():
    def plan(replay, session, thread, operations, *, key):
        interactions = []
        for index in range(len(operations) + 1):
            delta = {"content": "已核查目录与连接事实。"} if index == len(operations) else {
                "tool_calls": [_tool_call(call_index=index, name=operations[index][0], arguments=operations[index][1])],
            }
            interactions.append(build_cassette(
                asset_id=f"{key}-{index}", model="control-replay-model",
                url="https://opencode.ai/zen/v1/chat/completions",
                frames=chat_completion_frames(
                    response_id=f"chatcmpl-{key}-{index}", model="control-replay-model",
                    deltas=({"role": "assistant", **delta},), finish_reason="stop" if index == len(operations) else "tool_calls",
                ),
            ).raw["interactions"][0])
        return replay.control.plan_model_calls(session, thread, interactions)
    return plan


async def observe_operation(client, operation_id, status):
    async with asyncio.timeout(15):
        while True:
            response = await client.get(f"/api/v1/mcp/connection-operations/{operation_id}", params={"wait_timeout_ms": 100})
            assert response.status_code == 200, response.text
            operation = response.json()["data"]
            if operation["status"] == status:
                return operation
            assert operation["status"] not in {"succeeded", "failed", "expired", "cancelled"}, operation


async def test_initially_disabled_tool_is_known_but_never_reaches_server(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, discovery_plan,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, tool_restrictions={
            "execution_disabled": ["mcp__schema__probe"], "model_hidden": ["connections.manage"],
        }) as (client, replay, case),
    ):
        session = await create_session(client, "初始禁用仍可辨认")
        response = await client.get(f"/api/v1/sessions/{session}/main-thread")
        assert response.status_code == 200, response.text
        thread = response.json()["data"]["thread_id"]
        operations = [
            ("search_tools", {"tool_ids": ["mcp__schema__probe", "connections.manage", "missing"]}),
            ("invoke_extension_tool", {"tool_name": "mcp__schema__probe", "arguments": {"proof": "must-not-run"}}),
        ]
        calls = discovery_plan(replay, session, thread, operations, key="initial-discovery-policy")
        response = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "核查已知工具的禁用、隐藏与未知身份，并尝试一次受禁用策略保护的调用。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert response.status_code == 200, response.text
        await wait_for_job_done(client, response.json()["data"]["job_id"], max_attempts=100)
        records = [json.loads((replay.artifacts / f"{identity}.json").read_text())["request"] for identity in calls]
        results = [message for message in records[-1]["messages"] if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in results] == ["debug-e2e-call-0", "debug-e2e-call-1"]
        found = json.loads(results[0]["content"])["results"]
        assert [item["error"]["code"] for item in found] == ["tool-disabled", "tool-model-hidden", "tool-not-found"]
        assert all("input_schema" not in item and "description" not in item for item in found)
        assert "策略禁用" in results[1]["content"]
        assert not sibling.receipts.exists() or not sibling.receipts.read_text().strip()
        resolver = get_session_path_resolver(case.mount.workspace_root / ".boxteam/sessions")
        with RolloutCheckpointSaver(resolver.sessions_root) as saver:
            assemblies = saver.list_context_assemblies(session, thread)
            assert len(assemblies) == len(records) == 3
            for assembly, record in zip(assemblies, records, strict=True):
                wire = normalize_wire_request(record)
                assert assembly.request_hash_preimage == wire
                assert assembly.request_hash == context_request_hash(
                    assembly.as_sealed_plan(), assembly.provider_version, projector_id=assembly.projector_id,
                    projector_version=assembly.projector_version, target_format=assembly.target_format, wire_request=wire,
                )


async def test_previously_verified_oauth_catalog_remains_discoverable_without_execution(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, discovery_plan,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling) as (client, replay, case),
    ):
        secure.definitions.write_text(json.dumps({"probe": {
            "type": "object", "properties": {"proof": {"type": "string"}}, "required": ["proof"],
        }}))
        # 授权、目录验证和断开均由产品 API 生成；不预填 canonical／checkpoint 状态。
        for action, identity in (("connect", "known-first-connect"), ("disconnect", "known-disconnect")):
            response = await client.post("/api/v1/mcp/connection-operations", json={
                "operation_id": identity, "server_id": "secure", "action": action,
            })
            assert response.status_code == 200, response.text
            if action == "connect":
                await authorize(client, secure, identity)
            await observe_operation(client, identity, "succeeded")
        assert secure.exchanges == 1
        # 真实授权方撤销两个凭据，SDK 必须重新走授权，不能用现 token 伪报 ready。
        secure.access_token, secure.refresh_token = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        response = await client.post("/api/v1/mcp/connection-operations", json={
            "operation_id": "known-reauthorize", "server_id": "secure", "action": "connect",
        })
        assert response.status_code == 200, response.text
        await observe_operation(client, "known-reauthorize", "waiting_user")
        session = await create_session(client, "已知授权目录的真实状态")
        response = await client.get(f"/api/v1/sessions/{session}/main-thread")
        assert response.status_code == 200, response.text
        thread = response.json()["data"]["thread_id"]
        calls = discovery_plan(replay, session, thread, [
            ("search_tools", {"tool_ids": ["mcp__secure__probe", "mcp__never__probe", "mcp__schema__probe"]}),
            ("invoke_extension_tool", {"tool_name": "mcp__secure__probe", "arguments": {"proof": "must-not-run"}}),
            ("invoke_extension_tool", {"tool_name": "mcp__schema__probe", "arguments": {"proof": "sibling-ready"}}),
        ], key="known-unready")
        response = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "核查已知未授权目录、未知目标和就绪目标，并核对执行拒绝。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert response.status_code == 200, response.text
        await wait_for_job_done(client, response.json()["data"]["job_id"], max_attempts=100)
        records = [json.loads((replay.artifacts / f"{identity}.json").read_text())["request"] for identity in calls]
        results = [message for message in records[-1]["messages"] if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in results] == [f"debug-e2e-call-{index}" for index in range(3)]
        known, unknown, ready = json.loads(results[0]["content"])["results"]
        assert known["schema_state"] == "inline_complete" and known["input_schema"]["required"] == ["proof"]
        assert known["readiness"] == "authorization_required" and known["execution_ready"] is False
        assert known["missing_connections"] == ["secure"]
        assert known["next_control"]["tool_name"] == "connections.manage"
        assert unknown["error"]["code"] == "tool-not-found" and "连接" in unknown["error"]["message"]
        assert ready["readiness"] == "ready" and ready["execution_ready"] is True
        assert "连接未就绪" in results[1]["content"]
        assert not secure.receipts.exists() or not secure.receipts.read_text().strip()
        assert len(sibling.receipts.read_text().splitlines()) == 1
        assert secure.exchanges == 1
        resolver = get_session_path_resolver(case.mount.workspace_root / ".boxteam/sessions")
        with RolloutCheckpointSaver(resolver.sessions_root) as saver:
            assemblies = saver.list_context_assemblies(session, thread)
            assert len(assemblies) == len(records) == 4
            for assembly, record in zip(assemblies, records, strict=True):
                wire = normalize_wire_request(record)
                assert assembly.request_hash_preimage == wire
                assert assembly.request_hash == context_request_hash(
                    assembly.as_sealed_plan(), assembly.provider_version, projector_id=assembly.projector_id,
                    projector_version=assembly.projector_version, target_format=assembly.target_format, wire_request=wire,
                )
        assert (await client.post("/api/v1/mcp/connection-operations/known-reauthorize/cancel")).status_code == 200


async def test_upstream_authorization_uses_published_remote_manager_without_reconnecting_server(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, discovery_plan,
):
    async with oauth_endpoint_factory() as secure, http_schema_endpoint_factory("upstream") as endpoint:
        endpoint.definitions.write_text(json.dumps({name: {
            "type": "object", "properties": {"application": {"type": "string"}},
            "required": ["application"], "additionalProperties": False,
        } for name in ("upstream_query", "upstream_manage")}))
        async with product_case(secure, endpoint) as (client, replay, case):
            session = await create_session(client, "上游应用授权归实际Server")
            response = await client.get(f"/api/v1/sessions/{session}/main-thread")
            assert response.status_code == 200, response.text
            thread = response.json()["data"]["thread_id"]
            operations = [
                ("search_tools", {"tool_ids": [
                    "connections.manage", "mcp__schema__upstream_query", "mcp__schema__upstream_manage",
                    "mcp__schema__invented_authorization",
                ]}),
                ("invoke_extension_tool", {"tool_name": "mcp__schema__upstream_query", "arguments": {"application": "code_host"}}),
                ("invoke_extension_tool", {"tool_name": "connections.manage", "arguments": {
                    "request": {"operation": "authorize_app", "server_id": "schema", "application": "code_host"},
                }}),
                ("invoke_extension_tool", {"tool_name": "mcp__schema__upstream_manage", "arguments": {"application": "code_host"}}),
                ("invoke_extension_tool", {"tool_name": "connections.manage", "arguments": {
                    "request": {"operation": "list", "server_id": "schema"},
                }}),
            ]
            calls = discovery_plan(replay, session, thread, operations, key="upstream-authorization-owner")
            response = await client.post(f"/api/v1/sessions/{session}/messages", json={
                "message": {"content": "检查Server真实应用管理能力；上游应用授权不能用本地重连代替，不支持的能力明确报错。"},
                "run": {"mode": "single_agent", "agent_id": "default"},
            })
            assert response.status_code == 200, response.text
            await wait_for_job_done(client, response.json()["data"]["job_id"], max_attempts=120)
            records = [json.loads((replay.artifacts / f"{identity}.json").read_text())["request"] for identity in calls]
            results = [message for message in records[-1]["messages"] if message["role"] == "tool"]
            assert [message["tool_call_id"] for message in results] == [f"debug-e2e-call-{index}" for index in range(5)]
            found = json.loads(results[0]["content"])["results"]
            assert "重连不能证明上游应用已授权" in found[0]["description"]
            assert [item["tool_id"] for item in found[1:3]] == ["mcp__schema__upstream_query", "mcp__schema__upstream_manage"]
            assert found[3]["error"]["code"] == "tool-not-found" and "input_schema" not in found[3]
            for index, expected in ((1, "authorization_required"), (3, "waiting_user")):
                content, = results[index]["content"]
                state = json.loads(content["text"])
                assert state["authorization_layer"] == "upstream_app" and state["status"] == expected
                assert state["application"] == "code_host"
            assert "参数校验失败" in results[2]["content"]
            local = json.loads(results[4]["content"])
            assert local["authorization_layer"] == "server"
            connection, = local["connections"]
            assert connection["status"] == "ready"
            assert [json.loads(line) for line in endpoint.receipts.read_text().splitlines()] == [
                {"name": name, "arguments": {"application": "code_host"}}
                for name in ("upstream_query", "upstream_manage")
            ]
            assert secure.exchanges == 0
            resolver = get_session_path_resolver(case.mount.workspace_root / ".boxteam/sessions")
            with RolloutCheckpointSaver(resolver.sessions_root) as saver:
                items = saver._storage.storage.read_items(session, thread)
                attempts = [item for item in items if item.semantic_kind == "tool_result"
                            and item.projection_identity is not None
                            and item.projection_identity.projection_message_id is None]
                assert len(attempts) == 5
                assert [item.payload["tool_outcome"] for item in attempts] == ["success", "failure", "failure", "success", "success"]
                for item, index in zip((attempts[1], attempts[3]), (1, 3), strict=True):
                    assert item.payload["artifact"]["structured_content"] == json.loads(results[index]["content"][0]["text"])
                assemblies = saver.list_context_assemblies(session, thread)
                assert len(assemblies) == len(records) == 6
                for assembly, record in zip(assemblies, records, strict=True):
                    assert assembly.request_hash_preimage == normalize_wire_request(record)
                    assembly.validate_hashes()
