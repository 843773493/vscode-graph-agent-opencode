"""真实 Gateway 创建/执行/fork 后查询尚未打开的 durable child；模型使用统一回放。"""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    _job,
    _send,
    _tool_reply,
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    restart_source_case as restart_source_case,  # noqa: PLC0414 - 复用进程/回放生命周期
)
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)


@pytest.fixture(autouse=True)
def reserved_service_ports(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """在 Backend 及其 HTTP 客户端之前保留端口，覆盖 Gateway 启动与重启。"""
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    monkeypatch.setenv("BOXTEAM_GATEWAY_URL", f"http://127.0.0.1:{ports.port(20)}")
    lease = E2EProcessLease(
        context.runtime_root_for_node(request.node.nodeid) / "service-ports.json",
        tuple(ports.port(offset) for offset in (0, 20, 40, 42, 43)),
    )
    lease.acquire()
    try:
        yield
    finally:
        lease.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_source_case", [{"plugin_ids": ("terminal", "browser")}], indirect=True)
async def test_thread_discovery_through_gateway_survives_backend_restart(
    request: pytest.FixtureRequest,
    restart_source_case: RestartRuntimeSourceCase,
) -> None:
    case = restart_source_case
    backend = case.backends[-1]
    gateway = await asyncio.to_thread(
        start_gateway_process,
        workspace_root=case.workspace,
        default_backend_url=f"http://127.0.0.1:{backend.port}",
        port=integration_port_block_for_file(Path(request.node.path)).port(20),
    )
    original_client = case.client
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            await acquire_gateway_guest(client)
            health = await client.get("/api/gateway/health")
            registry = await client.get(
                "/api/gateway/workspaces", params={"check_health": False}
            )
            assert health.status_code == registry.status_code == 200
            for response in (health, registry):
                assert response.headers["X-Request-ID"] == response.json()["request_id"]
            owner = registry.json()["data"]["active_workspace"]
            assert owner == {"gateway_id": health.json()["data"]["gateway_id"], "workspace_id": backend.workspace_id}
            client.headers.update({
                "X-BoxTeam-Target-Gateway-Id": owner["gateway_id"],
                "X-BoxTeam-Workspace-Id": owner["workspace_id"],
            })
            case.client = client
            session = await create_session(client, "Same Discovery Name🧪")
            sibling = await create_session(client, "Same Discovery Name🧪")
            await case.plan(
                session, [
                    _tool_reply("invoke_extension_tool", {"tool_name": "test_tool_2", "arguments": {}}, "call_search_body"),
                    {"content": "canonical-source-for-thread-discovery"},
                ]
            )
            accepted = await _send(case, session, "通过产品执行创建 fork 来源")
            await _job(case, accepted["job_id"])
            point_response = await client.get(
                f"/api/v1/sessions/{session}/fork-source-point"
            )
            assert point_response.status_code == 200, point_response.text
            point = point_response.json()["data"]

            async def fork(key: str) -> dict:
                response = await client.post(
                    f"/api/v1/sessions/{session}/fork-context",
                    json={
                        "idempotency_key": key,
                        "mode": "full_rollout_copy",
                        "source_uri": point["source_uri"],
                        "source_view_id": point["source_view_id"],
                        "source_view_revision": point["source_view_revision"],
                        "target_container_uri": point["child_container_uri"],
                        "creation_kind": "new_child_thread",
                        "title": "Same Discovery Name🧪",
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()["data"]

            child = await fork("thread-discovery-first")

            capabilities = await client.get("/api/v1/search/capabilities")
            assert capabilities.status_code == 200, capabilities.text
            assert capabilities.headers["X-Request-ID"] == capabilities.json()["request_id"]
            workspace_id = capabilities.json()["data"]["workspace_id"]
            assert workspace_id == owner["workspace_id"]
            assert {(item["category"], operation) for item in capabilities.json()["data"]["providers"] for operation in item["operations"]} == {
                ("catalog", "name"), ("thread", "name"), ("message", "content"), ("tool_result", "content"),
                ("file", "name"), ("file", "content"), ("llm_request", "content"), ("context", "content"), ("trace", "content"),
            }
            assert {(item["category"], operation): item["match_modes"] for item in capabilities.json()["data"]["providers"]
                    for operation in item["operations"]} == {
                ("catalog", "name"): ["fuzzy"], ("thread", "name"): ["fuzzy"],
                ("message", "content"): ["literal"], ("tool_result", "content"): ["literal"],
                ("file", "name"): ["path", "fuzzy", "glob"], ("file", "content"): ["literal", "regex"],
                ("llm_request", "content"): ["literal"], ("context", "content"): ["literal", "regex"], ("trace", "content"): ["literal"],
            }
            payload = {"workspace_id": workspace_id, "category": "thread", "query": "SDN🧪", "limit": 1}
            unified = await client.post("/api/v1/search", json=payload)
            assert unified.status_code == 200, unified.text
            assert unified.json()["data"]["provider_id"] == "session-thread"
            assert unified.json()["data"]["state"] == "limited"
            for change, code in (
                ({"workspace_id": "019a0000-0000-7000-8000-000000000002"}, "owner_mismatch"),
                ({"category": "unregistered"}, "provider_unavailable"),
                ({"contract_version": 2}, "unsupported_version"),
                ({"match_mode": "regex"}, "unsupported_option"),
            ):
                rejected = await client.post("/api/v1/search", json={**payload, **change})
                assert rejected.status_code == 400, rejected.text
                assert rejected.json()["detail"]["code"] == code
                assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"]
            zero = await client.post("/api/v1/search", json={**payload, "limit": 0})
            assert zero.status_code == 200, zero.text
            assert zero.json()["data"]["limited_reason"] == "output_limit"
            assert zero.json()["data"]["threads"] is None

            catalog_search = await client.post("/api/v1/search", json={**payload, "category": "catalog"})
            direct_catalog = await client.get("/api/v1/session-catalog/search", params={"query": "SDN🧪", "limit": 1})
            assert catalog_search.status_code == direct_catalog.status_code == 200
            assert catalog_search.json()["data"]["catalog"] == direct_catalog.json()["data"]

            queries = [{"category": category, "operation": "content", "match_mode": "literal", "query": text, "limit": 1}
                       for category, text in (("message", "canonical-source-for-thread-discovery"), ("tool_result", "4568"))]

            async def federated(payload: dict, request_id: str, *, expected_results: int = 2) -> list[dict]:
                async with client.stream("POST", "/api/gateway/search", json=payload, headers={"X-Request-ID": request_id}) as response:
                    assert response.status_code == 200, (await response.aread()).decode()
                    assert response.headers["X-Request-ID"] == request_id
                    events = [json.loads(line.removeprefix("data: ")) async for line in response.aiter_lines() if line.startswith("data: ")]
                assert all(event["request_id"] == request_id for event in events)
                assert events[0]["type"] == "scope" and events[-1]["type"] == "end"
                assert events[-1]["summary"]["returned_results"] == expected_results
                assert {item["branch"]["category"] for item in events[-1]["summary"]["branches"]} == {
                    query["category"] for query in payload["queries"]}
                return events

            body_first = await federated({
                "scope": {"mode": "origin", "origin": owner},
                "queries": queries,
            }, "req_body_first")
            body_pages = [event for event in body_first if event["type"] == "page"]
            assert len(body_pages) == 2
            for event in body_pages:
                assert event["branch"]["returned_results"] == 1 and event["branch"]["cursor"]
                hit, = event["page"]["messages"]["items"]
                assert hit["thread"]["session_id"] == session and hit["thread"]["kind"] == "main"
                assert hit["match"]["reference"]["category"] == event["page"]["category"]

            message_page = next(event for event in body_pages if event["page"]["category"] == "message")
            tool_page = next(event for event in body_pages if event["page"]["category"] == "tool_result")
            body_scope = {"mode": "resume", "scope": body_first[0]["scope"]["scope"]}
            rejections = []
            for query, branch, cursor, reason in (
                (queries[0], message_page["branch"]["branch"], "MTIz", "malformed"),
                ({**queries[0], "query": "another-query"}, message_page["branch"]["branch"], message_page["branch"]["cursor"], "query_mismatch"),
                ({**queries[0], "category": "tool_result"}, tool_page["branch"]["branch"], message_page["branch"]["cursor"], "category_mismatch"),
            ):
                direct = await client.post("/api/v1/search", json={"workspace_id": workspace_id, **query, "cursor": cursor})
                assert direct.status_code == 400, direct.text
                detail = direct.json()["detail"]
                assert (detail["owner"], detail["code"], detail["reason"]) == (
                    "workspace_message_search", "invalid_turn_cursor", reason)
                assert direct.headers["X-Request-ID"] == direct.json()["request_id"]
                legacy = await client.post("/api/v1/sessions/history/search", json={
                    "query": query["query"], "category": query["category"], "limit": 1, "cursor": cursor})
                assert legacy.status_code == 400 and legacy.json()["detail"] == detail, legacy.text
                rejected = await federated({"scope": body_scope, "queries": [query],
                    "continuations": [{"branch": branch, "cursor": cursor}]}, "req_body_" + reason, expected_results=0)
                failure, = rejected[-1]["summary"]["branches"]
                assert (failure["code"], failure["error_owner"], failure["reason"], failure["http_status"]) == (
                    "invalid_turn_cursor", "workspace_message_search", reason, 400)
                assert failure["cursor"] == cursor and failure["retryable"]
                assert not any(event["type"] == "page" for event in rejected)
                rejections.append(rejected)

            async def search(cursor: str | None = None) -> httpx.Response:
                response = await client.get(
                    "/api/v1/session-threads/search",
                    params={
                        "query": "SDN🧪",
                        "limit": 1,
                        **({"cursor": cursor} if cursor else {}),
                    },
                )
                assert response.headers["X-Request-ID"] == response.json()["request_id"]
                return response

            first = await search()
            assert first.status_code == 200, first.text
            assert unified.json()["data"]["threads"] == first.json()["data"]
            assert first.json()["data"]["items"][0]["kind"] == "main"
            assert first.json()["data"]["items"][0]["session_id"] == session
            first_cursor = first.json()["data"]["cursor"]
            assert first_cursor
            calls_before = len(case.requests())
            await case.restart(hard=False)
            body_resumed = await federated({
                "scope": {"mode": "resume", "scope": body_first[0]["scope"]["scope"]}, "queries": queries,
                "continuations": [{"branch": event["branch"]["branch"], "cursor": event["branch"]["cursor"]} for event in body_pages],
            }, "req_body_after_restart")
            for event in body_resumed:
                if event["type"] != "page":
                    continue
                hit, = event["page"]["messages"]["items"]
                assert hit["thread"]["thread_uri"] == child["thread_uri"]
                located = await client.post(f"/api/v1/sessions/{session}/threads/{child['thread_id']}/history", json={
                    "direction": "around", "anchor_turn_id": hit["match"]["turn_id"], "anchor_message": hit["match"]["reference"],
                })
                assert located.status_code == 200, located.text
                expected = "4568" if event["page"]["category"] == "tool_result" else "canonical-source-for-thread-discovery"
                assert located.json()["data"]["located_message"]["content"] == expected
            second = await search(first_cursor)
            assert second.status_code == 200, second.text
            (hit,) = second.json()["data"]["items"]
            assert (hit["session_id"], hit["thread_id"], hit["kind"]) == (
                session,
                child["thread_id"],
                "child",
            )
            assert hit["thread_uri"] == child["thread_uri"]
            continued = await client.post("/api/v1/search", json={**payload, "cursor": first_cursor})
            assert continued.status_code == 200, continued.text
            assert continued.json()["data"]["threads"] == second.json()["data"]
            assert continued.headers["X-Request-ID"] == continued.json()["request_id"]
            assert hit["thread_uri"].startswith(
                f"boxteam://{health.json()['data']['gateway_id']}/"
            )
            third = await search(second.json()["data"]["cursor"])
            assert third.status_code == 200, third.text
            assert third.json()["data"]["complete"]
            assert third.json()["data"]["cursor"] is None
            assert third.json()["data"]["items"][0]["session_id"] == sibling
            assert len(case.requests()) == calls_before
            # 当前 Session 的发布版本变化必须拒绝旧页，不将 child 退化为 main。
            await fork("thread-discovery-second")
            stale = await search(first_cursor)
            assert stale.status_code == 409 and "Thread 目录已更新" in stale.text, (
                stale.text
            )
            stale_body = await federated({"scope": body_scope, "queries": [queries[0]],
                "continuations": [{"branch": message_page["branch"]["branch"], "cursor": message_page["branch"]["cursor"]}]},
                "req_body_thread_changed", expected_results=0)
            thread_failure, = stale_body[-1]["summary"]["branches"]
            assert (thread_failure["code"], thread_failure["error_owner"], thread_failure["reason"], thread_failure["http_status"]) == (
                "stale_thread_catalog", "session_thread_catalog", "thread_revision", 409)
            assert thread_failure["cursor"] == message_page["branch"]["cursor"] and thread_failure["retryable"]
            await create_session(client, "Catalog revision changed after message search")
            stale_catalog = await federated({"scope": body_scope, "queries": [queries[0]],
                "continuations": [{"branch": message_page["branch"]["branch"], "cursor": message_page["branch"]["cursor"]}]},
                "req_body_catalog_changed", expected_results=0)
            catalog_failure, = stale_catalog[-1]["summary"]["branches"]
            assert (catalog_failure["code"], catalog_failure["error_owner"], catalog_failure["reason"], catalog_failure["http_status"]) == (
                "stale_message_search_cursor", "workspace_message_search", "catalog_revision", 409)
            assert catalog_failure["cursor"] == message_page["branch"]["cursor"] and catalog_failure["retryable"]
            retried = await federated({"scope": body_scope, "queries": [queries[0]],
                "continuations": [{"branch": message_page["branch"]["branch"]}]}, "req_body_explicit_retry", expected_results=1)
            assert all(branch["state"] != "failed" for branch in retried[-1]["summary"]["branches"])
            assert len(case.requests()) == calls_before
            case.write_actual(
                "thread-discovery",
                {
                    "first": first.json(),
                    "second": second.json(),
                    "third": third.json(),
                    "stale": stale.json(),
                    "child": child,
                    "model_calls": calls_before,
                    "body_first": body_first,
                    "body_resumed": body_resumed,
                    "cursor_rejections": rejections,
                    "stale_body": stale_body,
                    "stale_catalog": stale_catalog,
                    "retried": retried,
                },
            )
    finally:
        case.client = original_client
        await asyncio.to_thread(close_gateway_process, gateway)
