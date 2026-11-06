"""调度的时序、配额、整页交付及安全结算；这里不证明真实网络和领域匹配。"""

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic

import pytest

from app.core.resource_uri import thread_resource_uri
from app.gateway.search.scheduler import (
    SearchBranchError,
    SearchJob,
    SearchPage,
    plan_search,
    search_events,
)
from app.gateway.search.scope import SearchScopeError
from app.schemas.gateway.search import (
    GatewaySearchScopeDTO,
    GatewaySearchScopeResolutionDTO,
)
from app.schemas.internal_v2.resource_navigation import ResourceNavigationResultDTO
from app.schemas.internal_v2.search import WorkspaceSearchResultDTO
from app.schemas.internal_v2.session_navigation import (
    SessionCatalogSearchResultDTO,
    SessionCatalogSearchResultsDTO,
)
from app.schemas.internal_v2.turn_search import WorkspaceMessageSearchResultsDTO
from app.schemas.search_federation import (
    FederatedSearchRequest,
    FederatedSearchSourceDTO,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget


def test_file_roots_keep_local_options_and_independent_continuations(scope: GatewaySearchScopeResolutionDTO) -> None:
    targets = [{"owner": scope.scope.owners[0], "target": {"uri": f"boxteam://workspace/00000000-0000-4000-8000-000000000000/resources/files/root/{root}",
                "search_options": {"include_globs": [pattern]}}} for root, pattern in (("one", "*.txt"), ("two", "*.md"))]
    request = FederatedSearchRequest.model_validate({"scope": {"mode": "resume", "scope": scope.scope},
        "queries": [{"category": "file", "query": "needle", "file_targets": targets}], "max_results": 3})
    jobs = plan_search(request, scope)
    assert len(jobs) == 2 and [job.request.limit for job in jobs] == [2, 1]
    assert [job.request.file.search_options.include_globs for job in jobs] == [("*.txt",), ("*.md",)]
    resumed = FederatedSearchRequest.model_validate({**request.model_dump(), "continuations": [{"branch": jobs[1].key, "cursor": "root-two-cursor"}]})
    next_jobs = plan_search(resumed, scope)
    assert len(next_jobs) == 1 and next_jobs[0].request.cursor == "root-two-cursor"
    assert next_jobs[0].request.file == jobs[1].request.file
    wrong = resumed.continuations[0].branch.model_copy(update={"resource_uri": "boxteam://workspace/00000000-0000-4000-8000-000000000000/resources/files/root/other"})
    with pytest.raises(SearchScopeError, match="目录根"):
        plan_search(FederatedSearchRequest.model_validate({**request.model_dump(), "continuations": [{"branch": wrong}]}), scope)
    targets[0]["owner"] = {"gateway_id": "gateway_0123456789abcdef0123456789abcdef", "workspace_id": "00000000-0000-4000-8000-999999999998"}
    with pytest.raises(SearchScopeError, match="工作区集合"):
        plan_search(FederatedSearchRequest.model_validate({**request.model_dump(), "queries": [{"category": "file", "query": "needle", "file_targets": targets}]}), scope)


@pytest.mark.parametrize("operation", ["name", "content"])
async def test_file_page_counts_domain_units_and_rejects_wrong_root(scope: GatewaySearchScopeResolutionDTO, operation: str) -> None:
    root = "boxteam://workspace/00000000-0000-4000-8000-000000000000/resources/files/root/one"
    request = FederatedSearchRequest.model_validate({"scope": {"mode": "resume", "scope": scope.scope},
        "queries": [{"category": "file", "operation": operation, "query": "needle", "limit": 1,
                     "file_targets": [{"owner": scope.scope.owners[0], "target": {"uri": root}}]}]})
    jobs = plan_search(request, scope)

    async def query(job: SearchJob) -> SearchPage:
        return SearchPage(WorkspaceSearchResultDTO(
            provider_id="files", workspace_id=job.key.owner.workspace_id, category="file", operation=operation,
            contract_version=1, state="limited", limited_reason="result_limit", files=ResourceNavigationResultDTO.model_validate({
                "node": {"uri": root, "resource_shape": "directory", "operations": ["search"]},
                "entries": [] if operation == "content" else [{"uri": root + "/a.txt", "resource_shape": "file", "operations": ["read"]}],
                "matches": [{"uri": root + "/a.txt", "line": 1, "column": 1, "text": "needle", "snippet_start_column": 1}] if operation == "content" else [],
                "contexts": [{"uri": root + "/a.txt", "line": 2, "text": "context"}], "truncated": True, "next_cursor": "original-root-cursor",
                "truncation_reason": "entry_limit",
            }),
        ))

    events = [event async for event in search_events(jobs, scope, request_id="files", query=query, budget=NameSearchBudget.create(None))]
    branch, = events[-1].summary.branches
    assert branch.returned_results == 1 and branch.cursor == "original-root-cursor"

    async def wrong_root(job: SearchJob) -> SearchPage:
        result = await query(job)
        result.result.files = result.result.files.model_copy(update={"node": result.result.files.node.model_copy(update={"uri": root + "/wrong"})})
        return result

    invalid = [event async for event in search_events(jobs, scope, request_id="wrong-root", query=wrong_root, budget=NameSearchBudget.create(None))]
    assert not any(event.type == "page" for event in invalid)
    assert invalid[-1].summary.branches[0].code == "invalid_response" and invalid[-1].summary.branches[0].cursor is None


@pytest.fixture
def scope() -> GatewaySearchScopeResolutionDTO:
    owners = [{"gateway_id": "gateway_0123456789abcdef0123456789abcdef", "workspace_id": f"00000000-0000-4000-8000-{index:012d}"} for index in range(7)]
    return GatewaySearchScopeResolutionDTO(
        scope=GatewaySearchScopeDTO(gateway_id="gateway_0123456789abcdef0123456789abcdef", resolved_at=datetime.now(UTC), mode="all", owners=owners),
        branches=[{"owner": owner, "target_id": f"route-{index}", "state": "registered"} for index, owner in enumerate(owners)],
    )


@pytest.fixture
def request_body(scope: GatewaySearchScopeResolutionDTO) -> FederatedSearchRequest:
    return FederatedSearchRequest.model_validate({
        "scope": {"mode": "resume", "scope": scope.scope},
        "queries": [{"category": "catalog", "query": "needle"}], "max_results": 10,
    })


def page(job: SearchJob, *, cursor: str | None = None) -> SearchPage:
    return SearchPage(WorkspaceSearchResultDTO(
        provider_id="catalog", workspace_id=job.key.owner.workspace_id,
        category=job.key.category, operation=job.key.operation, contract_version=1,
        state="limited" if cursor else "complete", limited_reason="result_limit" if cursor else None,
        catalog=SessionCatalogSearchResultsDTO(revision="r1", catalog_revision=1, cursor=cursor),
    ))


async def test_fast_page_precedes_slow_branch_and_terminal_is_unique(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest,
) -> None:
    release = asyncio.Event()
    entered: list[str] = []

    async def query(job: SearchJob) -> SearchPage:
        entered.append(job.key.owner.workspace_id)
        if job.key.owner.workspace_id == "00000000-0000-4000-8000-000000000000":
            await release.wait()
        if job.key.owner.workspace_id == "00000000-0000-4000-8000-000000000001":
            raise SearchBranchError("target_unavailable", "B 离线", retryable=True)
        return page(job, cursor="owner-cursor")

    events = search_events(plan_search(request_body, scope), scope, request_id="req-1", query=query, budget=NameSearchBudget.create(None))
    first = await anext(events)
    assert first.type == "scope" and entered == []
    received = [first]
    while not any(event.type == "page" for event in received):
        received.append(await anext(events))
    assert received[-1].branch.branch.owner.workspace_id == "00000000-0000-4000-8000-000000000002"
    assert not release.is_set()
    # 慢消费不补排队分支，最多三份原页占用内存。
    for _ in range(4):
        await asyncio.sleep(0)
    assert entered == ["00000000-0000-4000-8000-000000000000", "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]
    release.set()
    received.extend([event async for event in events])
    assert [event.sequence for event in received] == list(range(1, len(received) + 1))
    assert {event.request_id for event in received} == {"req-1"}
    assert [event.type for event in received].count("end") == 1 and received[-1].type == "end"
    summary = received[-1].summary
    assert summary.state == "incomplete" and len(summary.branches) == 7
    assert [item.branch.owner.workspace_id for item in summary.branches] == [f"00000000-0000-4000-8000-{index:012d}" for index in range(7)]
    assert summary.branches[1].code == "target_unavailable"
    assert len([event for event in received if event.type == "branch"]) == 7


async def test_local_diagnostics_separate_queue_from_owner_and_keep_actual_source(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = asyncio.Event()
    entered: list[SearchJob] = []
    caplog.set_level(logging.DEBUG, logger="app.gateway.search.scheduler")

    async def query(job: SearchJob) -> SearchPage:
        entered.append(job)
        if len(entered) <= 3:
            await release.wait()
        result = page(job)
        assert isinstance(result.result, WorkspaceSearchResultDTO) and result.result.catalog is not None
        result.result.catalog.items = [SessionCatalogSearchResultDTO.model_validate({
            "node": {"node_id": "one", "kind": "folder", "name": "needle", "node_revision": 1},
            "relative_path": "needle",
        })]
        if job == entered[0]:
            result = replace(result, source=FederatedSearchSourceDTO(kind="gateway_catalog_mirror",
                updated_at=datetime(2026, 11, 1, tzinfo=UTC), stale=True, unavailable_reason="目标停止"))
        return result

    jobs = plan_search(request_body, scope)
    events = search_events(jobs, scope, request_id="local-diagnostics", query=query, budget=NameSearchBudget.create(None))
    assert (await anext(events)).type == "scope"
    next_event = asyncio.create_task(anext(events))
    while len(entered) < 3:
        await asyncio.sleep(0)
    # 真实阻塞原并发槽位；后面的 owner 尚未发起读取。
    await asyncio.sleep(0.02)
    release.set()
    await next_event
    remaining = [event async for event in events]
    records = [record.args for record in caplog.records
               if record.name == "app.gateway.search.scheduler" and isinstance(record.args, dict)]
    assert len(records) == len(jobs)
    assert {record["request_id"] for record in records} == {"local-diagnostics"}
    assert {record["branch"] for record in records} == {job.key for job in jobs}
    assert all(record["provider_id"] == "catalog" and record["received_results"] == 1 for record in records)
    queued: list[float] = []
    first: list[float] = []
    for record in records:
        queue_ms, provider_ms = record["queue_ms"], record["provider_ms"]
        assert isinstance(queue_ms, float) and isinstance(provider_ms, float)
        assert queue_ms >= 0 and provider_ms >= 0
        if record["branch"] in {job.key for job in jobs[3:]}:
            queued.append(queue_ms)
        else:
            first.append(provider_ms)
    assert min(queued) >= 20 and min(first) >= 20
    mirror, = [record for record in records if record["branch"] == jobs[0].key]
    assert mirror["source"] == "gateway_catalog_mirror" and mirror["stale"] is True
    assert all(record["source"] == "owner" and record["stale"] is False
               for record in records if record is not mirror)
    assert all("needle" not in record.message for record in caplog.records)
    summary = remaining[-1].summary
    assert summary is not None and summary.state == "complete" and summary.returned_results == len(jobs)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_local_diagnostics_do_not_fabricate_page_metrics_after_failure(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest,
    caplog: pytest.LogCaptureFixture, cancelled: bool,
) -> None:
    caplog.set_level(logging.DEBUG, logger="app.gateway.search.scheduler")

    async def query(job: SearchJob) -> SearchPage:
        if cancelled:
            raise asyncio.CancelledError()
        raise SearchBranchError("target_unavailable", "原连接失败", retryable=True)

    job = plan_search(request_body, scope)[0]
    events = [event async for event in search_events([job], scope, request_id="failed-diagnostics", query=query,
                                                    budget=NameSearchBudget.create(None))]
    record, = [record.args for record in caplog.records
               if record.name == "app.gateway.search.scheduler" and isinstance(record.args, dict)]
    assert record["branch"] == job.key and record["request_id"] == "failed-diagnostics"
    queue_ms, provider_ms = record["queue_ms"], record["provider_ms"]
    assert isinstance(queue_ms, float) and isinstance(provider_ms, float)
    assert queue_ms >= 0 and provider_ms >= 0
    assert all(record[field] is None for field in ("provider_id", "state", "received_results", "source", "stale"))
    summary = events[-1].summary
    assert summary is not None
    branch, = summary.branches
    assert branch.state == ("cancelled" if cancelled else "failed")
    assert branch.code == ("cancelled" if cancelled else "target_unavailable")
    assert branch.returned_results == 0 and branch.http_status is None
    caplog.clear()
    skipped = replace(job, request=job.request.model_copy(update={"limit": 0}))
    events = [event async for event in search_events([skipped], scope, request_id="not-dispatched", query=query,
                                                    budget=NameSearchBudget.create(None))]
    assert not any(record.name == "app.gateway.search.scheduler" for record in caplog.records)
    summary = events[-1].summary
    assert summary is not None and summary.branches[0].code == "output_limit"


async def test_deadline_includes_queue_and_waits_for_cancelled_owner_cleanup(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest,
) -> None:
    entered: list[str] = []
    cleaned: list[str] = []

    async def query(job: SearchJob) -> SearchPage:
        entered.append(job.key.owner.workspace_id)
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.append(job.key.owner.workspace_id)
        return page(job)

    events = [event async for event in search_events(
        plan_search(request_body, scope), scope, request_id="deadline", query=query,
        budget=NameSearchBudget(monotonic() + 0.03),
    )]
    assert len(entered) == len(cleaned) == 3
    assert all(branch.code == "deadline" for branch in events[-1].summary.branches)
    assert not any(event.type == "page" for event in events)


async def test_closing_stream_cancels_inflight_and_does_not_dispatch_queue(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()
    entered = []

    async def query(job: SearchJob) -> SearchPage:
        entered.append(job.key.owner.workspace_id)
        if job.key.owner.workspace_id == "00000000-0000-4000-8000-000000000000":
            return page(job)
        started.set()
        try:
            await release.wait()
        finally:
            settled.set()
        return page(job)

    events = search_events(plan_search(request_body, scope), scope, request_id="cancel", query=query, budget=NameSearchBudget.create(None))
    assert (await anext(events)).type == "scope"
    assert (await anext(events)).type == "page"
    await started.wait()
    await events.aclose()
    assert settled.is_set() and entered == ["00000000-0000-4000-8000-000000000000", "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]


@pytest.mark.parametrize("limit,expected", [(0, [0] * 7), (1, [1, 0, 0, 0, 0, 0, 0]), (10, [2, 2, 2, 1, 1, 1, 1])])
async def test_allocation_precedes_dispatch_and_zero_never_calls_owner(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest, limit: int, expected: list[int],
) -> None:
    jobs = plan_search(request_body.model_copy(update={"max_results": limit}), scope)
    assert [job.request.limit for job in jobs] == expected
    seen = []

    async def query(job: SearchJob) -> SearchPage:
        seen.append(job.request.limit)
        return page(job)

    result = [event async for event in search_events(jobs, scope, request_id="quota", query=query, budget=NameSearchBudget.create(None))]
    assert seen == [item for item in expected if item]
    assert [branch.code for branch in result[-1].summary.branches] == [None if item else "output_limit" for item in expected]


def test_continuation_is_original_owner_subset_and_preserves_cursor(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest,
) -> None:
    branch = {"owner": scope.scope.owners[1], "category": "catalog", "operation": "name"}
    resumed = FederatedSearchRequest.model_validate({**request_body.model_dump(), "continuations": [{"branch": branch, "cursor": "b-own-cursor"}]})
    jobs = plan_search(resumed, scope)
    assert len(jobs) == 1 and jobs[0].key.owner == scope.scope.owners[1] and jobs[0].request.cursor == "b-own-cursor"
    branch["owner"] = {"gateway_id": "gateway_0123456789abcdef0123456789abcdef", "workspace_id": "00000000-0000-4000-8000-999999999999"}
    wrong = FederatedSearchRequest.model_validate({**request_body.model_dump(), "continuations": [{"branch": branch}]})
    with pytest.raises(SearchScopeError, match="不属于"):
        plan_search(wrong, scope)


def test_default_order_is_category_then_frozen_scope(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest,
) -> None:
    request = FederatedSearchRequest.model_validate({**request_body.model_dump(), "queries": [
        {"category": "thread", "query": "needle"}, {"category": "catalog", "query": "needle"},
    ]})
    jobs = plan_search(request, scope)
    assert [job.key.category for job in jobs] == ["catalog"] * 7 + ["thread"] * 7
    assert [job.key.owner for job in jobs[:7]] == list(scope.scope.owners)


async def test_owner_ignoring_deadline_cancellation_cannot_publish_late_page(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest,
) -> None:
    job = plan_search(request_body, scope)[0]
    job = replace(job, request=job.request.model_copy(update={"cursor": "original-cursor"}))

    async def query(job: SearchJob) -> SearchPage:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return page(job, cursor="late-cursor")

    result = [event async for event in search_events([job], scope, request_id="late", query=query, budget=NameSearchBudget(monotonic() + 0.01))]
    assert not any(event.type == "page" for event in result)
    branch = result[-1].summary.branches[0]
    assert branch.code == "deadline" and branch.cursor == "original-cursor"


@pytest.mark.parametrize("corruption", ["owner", "count"])
async def test_invalid_page_never_publishes_or_advances_cursor(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest, corruption: str,
) -> None:
    job = plan_search(request_body, scope)[0]

    async def query(job: SearchJob) -> SearchPage:
        result = page(job, cursor="must-not-advance")
        if corruption == "owner":
            result.result.workspace_id = "another-workspace"
        else:
            result.result.catalog.items = [SessionCatalogSearchResultDTO.model_validate({
                "node": {"node_id": str(index), "kind": "folder", "name": "needle", "node_revision": 1},
                "relative_path": "needle",
            }) for index in range(job.request.limit + 1)]
        return result

    result = [event async for event in search_events([job], scope, request_id="invalid", query=query, budget=NameSearchBudget.create(None))]
    assert not any(event.type == "page" for event in result)
    assert result[-1].summary.state == "incomplete"
    branch = result[-1].summary.branches[0]
    assert branch.cursor is None and branch.code == "invalid_response"


@pytest.mark.parametrize("category", ["message", "tool_result"])
@pytest.mark.parametrize("exceeds_quota", [False, True])
async def test_content_page_counts_domain_hits_and_preserves_owner_continuation(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest, category: str, exceeds_quota: bool,
) -> None:
    request = FederatedSearchRequest.model_validate({**request_body.model_dump(), "queries": [{
        "category": category, "operation": "content", "match_mode": "literal", "query": "needle",
    }]})
    job = plan_search(request, scope)[0]

    async def query(job: SearchJob) -> SearchPage:
        hit = {"thread": {
            "workspace_id": job.key.owner.workspace_id, "session_id": "session", "thread_id": "child",
            "thread_uri": thread_resource_uri(job.key.owner.workspace_id, "session", "child", gateway_authority="gateway_0123456789abcdef0123456789abcdef"),
            "kind": "child", "title": "child", "session_title": "Session",
            "created_at": datetime.now(UTC),
        }, "match": {
            "turn_id": "turn", "message_id": None, "role": "tool" if category == "tool_result" else "assistant",
            "reference": {"item_id": "canonical", "content_hash": "version", "start_codepoint": 3,
                          "end_codepoint": 9, "category": category}, "preview": "…needle", "preview_start_codepoint": 2,
        }}
        domain = WorkspaceMessageSearchResultsDTO.model_validate({
            "catalog_revision": 1, "items": [hit] * (job.request.limit + 1 if exceeds_quota else 1),
            "cursor": "original-history-position", "complete": False, "limited_reason": "byte_limit",
            "scanned_sessions": 1, "scanned_threads": 1, "scanned_items": 1, "scanned_bytes": 100,
        })
        return SearchPage(WorkspaceSearchResultDTO(
            provider_id=f"canonical-{category}", workspace_id=job.key.owner.workspace_id, category=category,
            operation="content", contract_version=1, state="limited", limited_reason="byte_limit", messages=domain,
        ))

    events = [event async for event in search_events([job], scope, request_id="body", query=query, budget=NameSearchBudget.create(None))]
    branch, = events[-1].summary.branches
    if exceeds_quota:
        assert not any(event.type == "page" for event in events)
        assert branch.code == "invalid_response" and branch.cursor is None
        assert events[-1].summary.returned_results == 0
    else:
        assert len([event for event in events if event.type == "page"]) == 1
        assert branch.returned_results == events[-1].summary.returned_results == 1
        assert branch.code == "byte_limit" and branch.cursor == "original-history-position"
