"""真实 Gateway 创建/执行/fork 后查询尚未打开的 durable child；模型使用统一回放。"""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    _job,
    _send,
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
@pytest.mark.parametrize("restart_source_case", [{"native_services": True}], indirect=True)
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
            route = registry.json()["data"]["active_workspace_id"]
            client.headers["X-BoxTeam-Workspace-Id"] = route
            case.client = client
            session = await create_session(client, "Same Discovery Name🧪")
            sibling = await create_session(client, "Same Discovery Name🧪")
            await case.plan(
                session, [{"content": "canonical-source-for-thread-discovery"}]
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
            assert {(item["category"], operation) for item in capabilities.json()["data"]["providers"] for operation in item["operations"]} == {
                ("catalog", "name"), ("thread", "name"),
            }
            payload = {"workspace_id": workspace_id, "category": "thread", "query": "SDN🧪", "limit": 1}
            unified = await client.post("/api/v1/search", json=payload)
            assert unified.status_code == 200, unified.text
            assert unified.json()["data"]["provider_id"] == "session-thread"
            assert unified.json()["data"]["state"] == "limited"
            for change, code in (
                ({"workspace_id": "another-workspace"}, "owner_mismatch"),
                ({"category": "message"}, "provider_unavailable"),
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
            case.write_actual(
                "thread-discovery",
                {
                    "first": first.json(),
                    "second": second.json(),
                    "third": third.json(),
                    "stale": stale.json(),
                    "child": child,
                    "model_calls": calls_before,
                },
            )
    finally:
        case.client = original_client
        await asyncio.to_thread(close_gateway_process, gateway)
