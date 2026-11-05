"""原诊断来源经正式 ASGI 搜索及管理入口；不启动进程或替代日志匹配。"""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.credentials import FederationCredentialStore
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes.diagnostic_logs import router as diagnostics_router
from app.gateway.routes.search import router as search_router
from app.gateway.server.workspace_proxy import verify_user_access_for_proxy
from app.schemas.gateway.diagnostics import GatewayDiagnosticLogQueryResultDTO
from app.schemas.search_federation import FederatedSearchEventDTO

GATEWAY = "gateway_00000000000040008000000000000001"
A, B, SHARED = (f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 4))


@pytest.fixture
async def log_http(diagnostic_registry: GatewayWorkspaceRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr("app.gateway.auth.get_gateway_local_token", lambda: "logs-token")
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(search_router)
    app.include_router(diagnostics_router)
    app.state.registry = diagnostic_registry
    app.state.gateway_id = GATEWAY
    app.state.workspace_navigation_store = WorkspaceNavigationStore(storage_path=tmp_path / "navigation.json")
    app.dependency_overrides[verify_user_access_for_proxy] = lambda: None

    async def reject_workspace_transport(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"运行日志不得读工作区搜索：{request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject_workspace_transport)) as upstream:
        app.state.http_client = upstream
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://logs.test",
            headers={"X-Local-Token": "logs-token", "X-Request-ID": "req_logs"}) as client:
            yield client


def body(workspaces: tuple[str, ...] = (A, SHARED), *, source: str = "workspace", query: str = "sharedword", limit: int = 50) -> dict:
    return {"scope": {"mode": "selected", "owners": [{"gateway_id": GATEWAY, "workspace_id": workspace} for workspace in workspaces]},
            "queries": [{"category": "runtime_log", "operation": "content", "query": query, "match_mode": "literal", "limit": limit,
                         "diagnostic_targets": [{"gateway_id": GATEWAY, "scope": {"source": source,
                             "workspace_ids": list(workspaces) if source == "workspace" else []}}]}]}


def events(response: httpx.Response) -> list[FederatedSearchEventDTO]:
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == "req_logs"
    result = [FederatedSearchEventDTO.model_validate_json(line.removeprefix("data: "))
              for line in response.text.splitlines() if line.startswith("data: ")]
    assert [event.sequence for event in result] == list(range(1, len(result) + 1))
    assert all(event.request_id == "req_logs" for event in result)
    assert [event.type for event in result].count("end") == 1
    return result


async def test_shared_process_is_one_diagnostic_branch_and_one_hit(log_http: httpx.AsyncClient) -> None:
    actual = events(await log_http.post("/api/gateway/search", json=body()))
    assert [event.type for event in actual] == ["scope", "page", "branch", "end"]
    page = actual[1].diagnostic_logs
    assert page is not None and actual[1].page is None
    assert len(page.items) == 1 and page.items[0].source.workspace_ids == [A, SHARED]
    summary = actual[-1].summary
    assert summary is not None
    branch, = summary.branches
    assert branch.branch.diagnostics is not None
    assert branch.branch.owner is None and branch.branch.diagnostics.scope.workspace_ids == (A, SHARED)
    assert branch.returned_results == 1 and branch.state == "limited" and branch.code == "source_unavailable"
    assert "only-b" not in actual[1].model_dump_json() and "only-g" not in actual[1].model_dump_json()
    assert "root_path" not in actual[1].model_dump_json()


async def test_gateway_source_requires_separate_explicit_selection(log_http: httpx.AsyncClient) -> None:
    actual = events(await log_http.post("/api/gateway/search", json=body((A,), source="gateway")))
    page = actual[1].diagnostic_logs
    assert page is not None
    assert page.complete and {item.preview for item in page.items} == {"only-g sharedword", "launcher sharedword"}
    assert all(source.source == "gateway" and not source.workspace_ids for source in page.sources)
    summary = actual[-1].summary
    assert summary is not None and summary.state == "complete"


async def test_both_log_sources_keep_scope_quota_and_independent_continuation(log_http: httpx.AsyncClient) -> None:
    request = body((A,))
    request["queries"][0]["diagnostic_targets"].append({"gateway_id": GATEWAY, "scope": {"source": "gateway"}})
    request["max_results"] = 2
    first = events(await log_http.post("/api/gateway/search", json=request))
    summary, scope = first[-1].summary, first[0].scope
    assert summary is not None and scope is not None
    branches = summary.branches
    assert len(branches) == 2 and [branch.allocated_results for branch in branches] == [1, 1]
    assert all(branch.branch.owner is None for branch in branches)
    assert [branch.branch.diagnostics.scope.source for branch in branches if branch.branch.diagnostics is not None] == ["workspace", "gateway"]
    assert summary.returned_results == 2
    pages: dict[str, GatewayDiagnosticLogQueryResultDTO] = {}
    for event in first:
        if event.type == "page":
            assert event.branch is not None and event.branch.branch.diagnostics is not None
            assert event.diagnostic_logs is not None
            pages[event.branch.branch.diagnostics.scope.source] = event.diagnostic_logs
    assert set(pages) == {"workspace", "gateway"}
    assert pages["workspace"].scope.workspace_ids == (A,)
    assert [item.preview for item in pages["workspace"].items] == ["only-a sharedword 😀needle needle"]
    assert pages["workspace"].items[0].source.workspace_ids == [A, SHARED]
    assert all(item.source.source == "gateway" and not item.source.workspace_ids for item in pages["gateway"].items)
    gateway = branches[1]
    assert gateway.cursor and gateway.state == "limited"
    request.update(scope={"mode": "resume", "scope": scope.scope.model_dump(mode="json")},
                   continuations=[{"branch": gateway.branch.model_dump(mode="json"), "cursor": gateway.cursor}])
    second = events(await log_http.post("/api/gateway/search", json=request))
    resumed_summary = second[-1].summary
    assert resumed_summary is not None
    continued, = resumed_summary.branches
    assert continued.branch == gateway.branch and continued.allocated_results == 2
    assert continued.state == "complete" and continued.cursor is None
    next_page, = [event.diagnostic_logs for event in second if event.type == "page"]
    assert next_page is not None
    assert {item.preview for item in [*pages["gateway"].items, *next_page.items]} == {
        "only-g sharedword", "launcher sharedword"}


@pytest.mark.parametrize("source,workspaces", [("workspace", (A,)), ("workspace", (SHARED,)), ("gateway", ())])
async def test_same_gateway_same_log_source_is_rejected_even_with_different_workspace_filters(
    log_http: httpx.AsyncClient, source: str, workspaces: tuple[str, ...],
) -> None:
    request = body(source=source)
    request["queries"][0]["diagnostic_targets"].append({"gateway_id": GATEWAY, "scope": {
        "source": source, "workspace_ids": workspaces,
    }})
    response = await log_http.post("/api/gateway/search", json=request)
    assert response.status_code == 422 and "同一明确来源只能声明一次" in response.text


async def test_original_cursor_resumes_same_diagnostic_branch(log_http: httpx.AsyncClient) -> None:
    request = body((A,), query="needle", limit=1)
    first = events(await log_http.post("/api/gateway/search", json=request))
    summary, scope = first[-1].summary, first[0].scope
    assert summary is not None and scope is not None
    branch, = summary.branches
    assert branch.cursor
    request.update(scope={"mode": "resume", "scope": scope.scope.model_dump(mode="json")},
                   continuations=[{"branch": branch.branch.model_dump(mode="json"), "cursor": branch.cursor}])
    second = events(await log_http.post("/api/gateway/search", json=request))
    resumed_summary = second[-1].summary
    assert resumed_summary is not None and len(resumed_summary.branches) == 1
    first_page, second_page = first[1].diagnostic_logs, second[1].diagnostic_logs
    assert first_page is not None and second_page is not None
    assert second_page.items[0].reference.start_codepoint > first_page.items[0].reference.start_codepoint
    request["continuations"][0]["cursor"] = "broken"
    rejected = events(await log_http.post("/api/gateway/search", json=request))
    rejected_summary = rejected[-1].summary
    assert rejected_summary is not None
    failure, = rejected_summary.branches
    assert failure.state == "failed" and failure.code == "invalid_runtime_log_cursor"
    assert failure.error_owner == "runtime_log" and failure.reason == "malformed" and failure.cursor == "broken"
    assert not any(event.type == "page" for event in rejected)


async def test_zero_quota_does_not_open_logs(log_http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("零配额不得读取日志来源")
    monkeypatch.setattr("app.gateway.control.diagnostic_logs._sources", unexpected)
    actual = events(await log_http.post("/api/gateway/search", json={**body(), "max_results": 0}))
    assert [event.type for event in actual] == ["scope", "branch", "end"]
    summary = actual[-1].summary
    assert summary is not None
    branch, = summary.branches
    assert branch.state == "limited" and branch.code == "output_limit" and branch.allocated_results == 0


@pytest.mark.parametrize("damage", ["gateway", "workspace"])
async def test_diagnostic_target_cannot_escape_frozen_scope(log_http: httpx.AsyncClient, damage: str) -> None:
    request = body((A,))
    target = request["queries"][0]["diagnostic_targets"][0]
    target["gateway_id"] = "gateway_" + "f" * 32 if damage == "gateway" else GATEWAY
    target["scope"]["workspace_ids"] = [B] if damage == "workspace" else [A]
    response = await log_http.post("/api/gateway/search", json=request)
    assert response.status_code == 400 and response.json()["detail"]["code"] == "owner_mismatch"


async def test_named_query_locate_serialize_and_preserve_unicode(log_http: httpx.AsyncClient) -> None:
    query = {"scope": {"source": "workspace", "workspace_ids": [A]}, "query": "needle", "limit": 1}
    response = await log_http.post("/api/gateway/diagnostics/query", json=query)
    assert response.status_code == 200 and response.headers["X-Request-ID"] == response.json()["request_id"] == "req_logs"
    page = response.json()["data"]
    locate = {"scope": query["scope"], "reference": page["items"][0]["reference"]}
    response = await log_http.post("/api/gateway/diagnostics/locate", json=locate)
    assert response.status_code == 200 and response.json()["request_id"] == "req_logs"
    data = response.json()["data"]
    assert data["text"][data["reference"]["start_codepoint"]:data["reference"]["end_codepoint"]] == "needle"
    locate["reference"]["content_version"] = "0" * 64
    stale = await log_http.post("/api/gateway/diagnostics/locate", json=locate)
    assert stale.status_code == 409 and stale.json()["detail"] == {
        "owner": "runtime_log", "code": "stale_runtime_log_reference", "reason": "record_changed", "message": "原运行日志记录已改写"}


@pytest.mark.parametrize("path", ["/api/gateway/diagnostics/query", "/api/gateway/federation/diagnostics/query"])
async def test_named_management_preserves_original_auth_boundary(log_http: httpx.AsyncClient, path: str) -> None:
    query = {"scope": {"source": "gateway"}, "query": "sharedword"}
    denied = await log_http.post(path, json=query, headers={"X-Local-Token": "wrong"})
    assert denied.status_code == 401
    if "/federation/" in path:
        assert (await log_http.post(path, json=query)).status_code == 401
        from app.core.path_utils import get_gateway_root
        credential = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
            connection_id="log-peer", peer_gateway_id="gateway_" + "b" * 32)
        accepted = await log_http.post(path, json=query, headers={"X-BoxTeam-Federation-Token": credential.token})
        assert accepted.status_code == 200 and accepted.json()["data"]["gateway_id"] == GATEWAY
