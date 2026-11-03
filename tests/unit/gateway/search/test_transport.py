"""真实 ASGI／HTTP 编码与显式下游替身；不把这些检查称为实网验收。"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.gateway.control.catalog_search import (
    GatewaySessionCatalogSearchService,
    _CatalogSnapshot,
)
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.registry.owners import workspace_owner
from app.gateway.routes.search import router
from app.gateway.server.workspace_proxy import verify_user_access_for_proxy
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.search import WorkspaceSearchResultDTO
from app.schemas.internal_v2.session_navigation import (
    SessionCatalogNodeDTO,
    SessionCatalogSearchResultsDTO,
)
from app.schemas.search_federation import FederatedSearchEventDTO

GATEWAY = "gateway_search_transport"
OWNERS = [{"gateway_id": GATEWAY, "workspace_id": f"00000000-0000-4000-8000-{index:012d}"} for index in range(1, 4)]


class PausedBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.entered.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        await asyncio.sleep(0)
        self.closed.set()


@dataclass
class SearchHTTP:
    client: httpx.AsyncClient
    registry: GatewayWorkspaceRegistry
    requests: list[httpx.Request] = field(default_factory=list)
    mode: str = "normal"
    paused: PausedBody = field(default_factory=PausedBody)
    mirror: GatewaySessionCatalogSearchService | None = None
    network_error: type[httpx.RequestError] | None = None

    async def receive(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.network_error is not None:
            raise self.network_error("原网络故障详情", request=request)
        payload = json.loads(request.content)
        request_id = request.headers["X-Request-ID"]
        headers = {"X-Request-ID": request_id, "Content-Type": "application/json"}
        if self.mode == "paused":
            return httpx.Response(200, headers=headers, stream=self.paused)
        if self.mode in {"error", "wrong-error-id"}:
            if self.mode == "wrong-error-id":
                headers["X-Request-ID"] = "another-request"
            return httpx.Response(409, headers=headers, json={"detail": {"code": "owner_rejected", "message": "cursor 已过期"}})
        if self.mode == "provider-read-error":
            return httpx.Response(500, headers=headers, json={"detail": {
                "owner": "search_provider", "code": "provider_read_failed", "provider_id": "session-catalog",
                "category": "catalog", "operation": "name", "message": "原目录 SQLite 无法读取: errno=13",
            }})
        if self.mode in {"file-query-error", "file-cursor-error"}:
            reason = "invalid_search_pattern" if self.mode == "file-query-error" else "invalid_cursor"
            return httpx.Response(400, headers=headers, json={"detail": {
                "owner": "resource_file_operations", "code": "execution_failed", "reason": reason,
                "message": "文件查询被原 owner 拒绝",
            }})
        if self.mode in {"authentication-error", "permission-error"}:
            return httpx.Response(401 if self.mode == "authentication-error" else 403,
                                  headers=headers, json={"detail": "原访问权限拒绝"})
        if self.mode == "offline":
            raise httpx.ConnectError("目标断开", request=request)
        if self.mode == "wrong-id":
            headers["X-Request-ID"] = "another-request"
        if self.mode == "large":
            return httpx.Response(200, headers=headers, content=b"x" * (8 * 1024 * 1024 + 1))
        page = WorkspaceSearchResultDTO(
            provider_id="session-catalog", workspace_id=payload["workspace_id"] if self.mode != "wrong-owner" else "wrong",
            category=payload["category"], operation=payload["operation"], contract_version=1,
            state="limited", limited_reason="result_limit",
            catalog=SessionCatalogSearchResultsDTO(revision="r1", catalog_revision=1,
                cursor="续点🙂" * 2000 if self.mode == "long-cursor" else f"{payload['workspace_id']}-cursor"),
        )
        body = APIResponse(data=page, request_id=request_id).model_dump(mode="json")
        if self.mode == "invalid-page":
            body["data"]["contract_version"] = "broken-version"
        return httpx.Response(200, headers=headers, json=body)

    async def search(self, owners: list[dict] | None = None, **updates: object) -> httpx.Response:
        return await self.client.post("/api/gateway/search", json={
            "scope": {"mode": "selected", "owners": owners or OWNERS[:2]},
            "queries": [{"category": "catalog", "query": "needle"}], **updates,
        })


@pytest.fixture
async def search_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SearchHTTP]:
    monkeypatch.setattr("app.gateway.auth.get_gateway_local_token", lambda: "test-search-token")
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json")
    for index, owner in enumerate(OWNERS):
        registry.upsert(WorkspaceTarget(
            workspace_id=f"route_{index}", backend_workspace_id=owner["workspace_id"],
            name=f"Same project {index}", root_path=str(tmp_path / f"unused-{index}"),
            backend_url=f"http://owner-{index}.test", connection_kind="local",
        ), activate=False)
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(router)
    application.state.registry = registry
    application.state.workspace_navigation_store = WorkspaceNavigationStore(storage_path=tmp_path / "navigation.json")
    application.state.gateway_id = GATEWAY
    # 此装具的授权边界是明确替身；实网验收另走真实 guest 登录。
    application.dependency_overrides[verify_user_access_for_proxy] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://gateway.test",
                                 headers={"X-Local-Token": "test-search-token", "X-Request-ID": "req_search"}) as client:
        fixture = SearchHTTP(client, registry)
        async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.receive)) as upstream:
            application.state.http_client = upstream
            fixture.mirror = GatewaySessionCatalogSearchService(
                registry=registry, http_client=upstream, cache_dir=tmp_path / "mirrors", gateway_id=GATEWAY,
            )
            application.state.session_catalog_search_service = fixture.mirror
            try:
                yield fixture
            finally:
                registry.close()


def events(response: httpx.Response) -> list[FederatedSearchEventDTO]:
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == "req_search"
    assert response.headers["Content-Type"].startswith("text/event-stream")
    frames = [frame for frame in response.text.split("\n\n") if frame and not frame.startswith(":")]
    result = []
    for frame in frames:
        lines = frame.splitlines()
        event = FederatedSearchEventDTO.model_validate_json(next(line.removeprefix("data: ") for line in lines if line.startswith("data: ")))
        assert f"id: {event.sequence}" in lines and "event: search" in lines
        assert event.request_id == "req_search"
        result.append(event)
    assert result[0].type == "scope" and result[-1].type == "end"
    assert [item.sequence for item in result] == list(range(1, len(result) + 1))
    return result


async def test_only_explicit_a_b_receive_same_request_and_original_owner_cursor(search_http: SearchHTTP) -> None:
    result = events(await search_http.search())
    assert len(search_http.requests) == 2
    assert {request.url.host for request in search_http.requests} == {"owner-0.test", "owner-1.test"}
    for request in search_http.requests:
        payload = json.loads(request.content)
        assert request.url.path == "/api/v1/search"
        assert request.headers["X-Request-ID"] == "req_search"
        assert request.headers["X-BoxTeam-Gateway-Id"] == GATEWAY
        assert request.headers["X-BoxTeam-Workspace-Id"] == payload["workspace_id"]
        assert payload["deadline_at"] is not None and payload["limit"] == 50
    pages = [event for event in result if event.type == "page"]
    assert len(pages) == 2 and all(page.page.catalog.cursor == f"{page.branch.branch.owner.workspace_id}-cursor" for page in pages)
    assert result[-1].summary.state == "incomplete"
    assert all(search_http.registry.route_reference_counts(f"route_{index}") == (0, 0) for index in range(3))


async def test_unknown_owner_rejected_before_any_dispatch(search_http: SearchHTTP) -> None:
    response = await search_http.search([{**OWNERS[0], "workspace_id": "unknown"}])
    assert response.status_code == 404 and not search_http.requests
    assert response.json()["detail"]["code"] == "owner_unavailable"


@pytest.mark.parametrize("mode,code", [("wrong-id", "invalid_response"), ("wrong-owner", "invalid_response"),
                                       ("large", "delivery_limit"), ("error", "owner_rejected"), ("wrong-error-id", "invalid_response")])
async def test_failed_branch_has_no_page_or_advanced_cursor(search_http: SearchHTTP, mode: str, code: str) -> None:
    search_http.mode = mode
    result = events(await search_http.search(OWNERS[:1]))
    assert not any(event.type == "page" for event in result)
    assert result[-1].summary.branches[0].code == code
    assert result[-1].summary.branches[0].cursor is None
    assert search_http.registry.route_reference_counts("route_0") == (0, 0)


async def test_provider_read_error_keeps_identity_cause_and_original_continuation(search_http: SearchHTTP) -> None:
    first = events(await search_http.search(OWNERS[:1]))
    scope = first[0].scope
    branch = first[-1].summary
    assert scope is not None and branch is not None
    previous = branch.branches[0]
    search_http.mode = "provider-read-error"
    result = events(await search_http.search(scope={"mode": "resume", "scope": scope.scope.model_dump(mode="json")},
        continuations=[{"branch": previous.branch.model_dump(mode="json"), "cursor": previous.cursor}]))
    summary = result[-1].summary
    assert summary is not None
    failure, = summary.branches
    assert failure.state == "failed" and failure.code == "provider_read_failed"
    assert failure.provider_id == "session-catalog" and failure.message == "原目录 SQLite 无法读取: errno=13"
    assert failure.error_owner == "search_provider" and failure.http_status == 500 and failure.reason is None
    assert failure.cursor == previous.cursor and failure.retryable
    assert failure.returned_results == summary.returned_results == 0
    assert not any(event.type == "page" for event in result)


@pytest.mark.parametrize("mode,reason", [("file-query-error", "invalid_search_pattern"), ("file-cursor-error", "invalid_cursor")])
async def test_original_domain_reason_distinguishes_same_failure_code(search_http: SearchHTTP, mode: str, reason: str) -> None:
    search_http.mode = mode
    result = events(await search_http.search(OWNERS[:1]))
    summary = result[-1].summary
    assert summary is not None
    failure, = summary.branches
    assert failure.code == "execution_failed" and failure.reason == reason
    assert failure.error_owner == "resource_file_operations" and failure.http_status == 400
    assert failure.state == "failed" and failure.returned_results == summary.returned_results == 0
    terminal, = [event.branch for event in result if event.type == "branch"]
    assert terminal == failure


@pytest.mark.parametrize("mode,status", [("authentication-error", 401), ("permission-error", 403)])
async def test_plain_http_rejection_keeps_actual_status_without_invented_owner(search_http: SearchHTTP, mode: str, status: int) -> None:
    search_http.mode = mode
    summary = events(await search_http.search(OWNERS[:1]))[-1].summary
    assert summary is not None
    failure, = summary.branches
    assert failure.code == "owner_http_error" and failure.http_status == status
    assert failure.error_owner is None and failure.reason is None
    assert not failure.retryable


@pytest.mark.parametrize("error_type,code", [
    (httpx.ConnectTimeout, "deadline"), (httpx.ReadTimeout, "deadline"),
    (httpx.WriteTimeout, "deadline"), (httpx.PoolTimeout, "deadline"),
    (httpx.ConnectError, "target_unavailable"), (httpx.ReadError, "target_unavailable"),
    (httpx.WriteError, "target_unavailable"), (httpx.CloseError, "target_unavailable"),
    (httpx.RemoteProtocolError, "target_unavailable"), (httpx.LocalProtocolError, "target_unavailable"),
    (httpx.ProxyError, "target_unavailable"), (httpx.UnsupportedProtocol, "target_unavailable"),
    (httpx.DecodingError, "target_unavailable"), (httpx.TooManyRedirects, "target_unavailable"),
])
async def test_network_failure_preserves_transport_reason_and_original_cursor(
    search_http: SearchHTTP, error_type: type[httpx.RequestError], code: str,
) -> None:
    original = events(await search_http.search(OWNERS[:1]))
    scope, summary = original[0].scope, original[-1].summary
    assert scope is not None and summary is not None
    previous, = summary.branches
    search_http.network_error = error_type
    result = events(await search_http.search(scope={"mode": "resume", "scope": scope.scope.model_dump(mode="json")},
        continuations=[{"branch": previous.branch.model_dump(mode="json"), "cursor": previous.cursor}]))
    summary = result[-1].summary
    assert summary is not None
    failure, = summary.branches
    assert failure.code == code and failure.state == "failed" and failure.retryable
    assert failure.error_owner == "search_transport" and failure.reason == error_type.__name__
    assert failure.http_status is None and failure.provider_id is None
    assert failure.message is not None and "原网络故障详情" in failure.message
    assert failure.cursor == previous.cursor and failure.returned_results == summary.returned_results == 0
    assert [event.branch for event in result if event.type == "branch"] == [failure]
    assert not any(event.type == "page" for event in result)
    assert search_http.registry.route_reference_counts("route_0") == (0, 0)
    search_http.network_error = None
    retried = events(await search_http.search(scope={"mode": "resume", "scope": scope.scope.model_dump(mode="json")},
        continuations=[{"branch": previous.branch.model_dump(mode="json"), "cursor": failure.cursor}]))
    page, = [event.page for event in retried if event.type == "page"]
    assert page is not None and page.provider_id == "session-catalog"
    assert json.loads(search_http.requests[-1].content)["cursor"] == previous.cursor


async def test_route_invalidation_closes_body_before_releasing_reference(search_http: SearchHTTP) -> None:
    search_http.mode = "paused"
    response = asyncio.create_task(search_http.search(OWNERS[:1]))
    await search_http.paused.entered.wait()
    assert search_http.registry.route_reference_counts("route_0") == (1, 0)
    search_http.registry.invalidate_route("route_0")
    result = events(await response)
    assert search_http.paused.closed.is_set()
    assert search_http.registry.route_reference_counts("route_0") == (0, 0)
    assert result[-1].summary.branches[0].code == "route_invalidated"
    assert not any(event.type == "page" for event in result)


async def test_cancellation_closes_body_and_releases_without_late_response(search_http: SearchHTTP) -> None:
    search_http.mode = "paused"
    response = asyncio.create_task(search_http.search(OWNERS[:1]))
    await search_http.paused.entered.wait()
    response.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response
    assert search_http.paused.closed.is_set()
    assert search_http.registry.route_reference_counts("route_0") == (0, 0)


async def test_zero_quota_and_missing_auth_do_not_read_owner(search_http: SearchHTTP) -> None:
    result = events(await search_http.search(max_results=0))
    assert not search_http.requests
    assert all(branch.code == "output_limit" for branch in result[-1].summary.branches)
    search_http.client.headers.pop("X-Local-Token")
    assert (await search_http.search()).status_code == 401
    assert not search_http.requests


async def test_output_budget_includes_repeated_cursors_and_retry_keeps_original_position(
    search_http: SearchHTTP, monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_http.mode = "long-cursor"
    continuations = [{"branch": {"owner": owner, "category": "catalog"}, "cursor": "previous-position"} for owner in OWNERS[:2]]
    resolved = await search_http.client.post("/api/gateway/search/resolve", json={"mode": "selected", "owners": OWNERS[:2]})
    assert resolved.status_code == 200
    scope = {"mode": "resume", "scope": resolved.json()["data"]["scope"]}
    with monkeypatch.context() as limits:
        limits.setattr("app.gateway.search.output.MAX_SEARCH_OUTPUT_BYTES", 45000)
        response = await search_http.search(scope=scope, continuations=continuations)
    limited = events(response)
    assert len(response.content) <= 45000
    assert not any(event.type == "page" for event in limited)
    summary = limited[-1].summary
    assert summary is not None
    assert all(branch.state == "limited" and branch.code == "output_bytes" and branch.cursor == "previous-position"
               for branch in summary.branches)
    retried = events(await search_http.search(scope=scope, continuations=continuations))
    assert len([event for event in retried if event.type == "page"]) == 2
    assert all(json.loads(request.content)["cursor"] == "previous-position" for request in search_http.requests)
    retry_summary = retried[-1].summary
    assert retry_summary is not None
    assert all(branch.cursor == "续点🙂" * 2000 for branch in retry_summary.branches)


async def test_control_budget_rejected_before_http_dispatch(search_http: SearchHTTP, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.gateway.search.output.MAX_SEARCH_OUTPUT_BYTES", 1024)
    response = await search_http.search()
    assert response.status_code == 413 and not search_http.requests
    assert response.json()["detail"]["code"] == "output_bytes"
    assert response.headers["X-Request-ID"] == "req_search"


@pytest.fixture
def mirrored_owner(search_http: SearchHTTP) -> _CatalogSnapshot:
    assert search_http.mirror is not None
    owner = workspace_owner(search_http.registry.resolve("route_0"), search_http.registry, GATEWAY)
    nodes = [SessionCatalogNodeDTO(node_id=f"folder-{index}", kind="folder", name=f"needle {index}", node_revision=1)
             for index in range(3)]
    snapshot = _CatalogSnapshot(owner=owner, revision="mirror-v1", catalog_revision=7,
                                updated_at=datetime.now(UTC), nodes_by_id={node.node_id: node for node in nodes})
    search_http.mirror._snapshots[owner] = snapshot
    return snapshot


async def test_mirror_is_opt_in_and_online_always_uses_owner(search_http: SearchHTTP, mirrored_owner: _CatalogSnapshot) -> None:
    search_http.mode = "offline"
    failed = events(await search_http.search(OWNERS[:1]))
    assert failed[-1].summary.branches[0].code == "target_unavailable"
    assert failed[-1].summary.branches[0].http_status is None
    assert not any(event.type == "page" for event in failed)
    query = {"category": "catalog", "query": "needle", "limit": 1, "allow_catalog_mirror": True}
    offline = events(await search_http.search(OWNERS[:1], queries=[query]))
    page, = [event for event in offline if event.type == "page"]
    assert page.source.kind == "gateway_catalog_mirror" and page.source.stale
    assert page.source.updated_at == mirrored_owner.updated_at and "目标断开" in page.source.unavailable_reason
    assert page.page.catalog.catalog_revision == 7
    cursor = page.page.catalog.cursor
    resumed = events(await search_http.search(scope={"mode": "resume", "scope": offline[0].scope.scope.model_dump(mode="json")},
        queries=[query], continuations=[{"branch": page.branch.branch.model_dump(mode="json"), "cursor": cursor}]))
    next_page, = [event for event in resumed if event.type == "page"]
    assert next_page.page.catalog.items[0].node.node_id == "folder-1"
    search_http.mode = "normal"
    online = events(await search_http.search(OWNERS[:1], queries=[query]))
    live, = [event for event in online if event.type == "page"]
    assert live.source.kind == "owner" and not live.source.stale
    assert live.page.catalog.revision == "r1"


async def test_mirror_never_masks_owner_rejection_or_route_reuse(search_http: SearchHTTP, mirrored_owner: _CatalogSnapshot) -> None:
    query = {"category": "catalog", "query": "needle", "allow_catalog_mirror": True}
    search_http.mode = "error"
    rejected = events(await search_http.search(OWNERS[:1], queries=[query]))
    assert rejected[-1].summary.branches[0].code == "owner_rejected"
    assert not any(event.type == "page" for event in rejected)
    search_http.mode = "offline"
    old = search_http.registry.resolve("route_0")
    from dataclasses import replace
    search_http.registry.upsert(replace(old, backend_workspace_id="00000000-0000-4000-8000-000000000099"))
    response = await search_http.search(OWNERS[:1], queries=[query])
    assert response.status_code == 404 and response.json()["detail"]["code"] == "owner_unavailable"


@pytest.mark.parametrize("sink_failed", [False, True])
async def test_invalid_provider_page_preserves_diagnostic_and_branch_failure(
    search_http: SearchHTTP, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, sink_failed: bool,
) -> None:
    search_http.mode = "invalid-page"
    errors = []

    def reject_log(_message, _detail, *, exc_info, extra):
        errors.append(exc_info)
        raise OSError("diagnostic unavailable")

    if sink_failed:
        monkeypatch.setattr("app.gateway.search.transport.logger.error", reject_log)
    result = events(await search_http.search())
    assert result[-1].summary is not None
    assert all(branch.code == "invalid_response" and branch.state == "failed" and branch.cursor is None
               for branch in result[-1].summary.branches)
    if sink_failed:
        assert len(errors) == 2 and all(error.__notes__ for error in errors)
        assert all("diagnostic_sink_error=OSError" in branch.message for branch in result[-1].summary.branches)
    else:
        assert any(record.exc_info and "broken-version" in str(record.exc_info[1]) for record in caplog.records)
    assert all(search_http.registry.route_reference_counts(f"route_{index}") == (0, 0) for index in range(3))
