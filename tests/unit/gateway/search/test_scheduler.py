"""调度的时序、配额、整页交付及安全结算；这里不证明真实网络和领域匹配。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic

import pytest

from app.gateway.search.scheduler import (
    SearchBranchError,
    SearchJob,
    plan_search,
    search_events,
)
from app.gateway.search.scope import SearchScopeError
from app.schemas.gateway.search import (
    GatewaySearchScopeDTO,
    GatewaySearchScopeResolutionDTO,
)
from app.schemas.internal_v2.search import WorkspaceSearchResultDTO
from app.schemas.internal_v2.session_navigation import (
    SessionCatalogSearchResultDTO,
    SessionCatalogSearchResultsDTO,
)
from app.schemas.search_federation import FederatedSearchRequest
from app.services.business.session_navigation.name_search_budget import NameSearchBudget


@pytest.fixture
def scope() -> GatewaySearchScopeResolutionDTO:
    owners = [{"gateway_id": "gw", "workspace_id": f"ws-{index}"} for index in range(7)]
    return GatewaySearchScopeResolutionDTO(
        scope=GatewaySearchScopeDTO(gateway_id="gw", resolved_at=datetime.now(UTC), mode="all", owners=owners),
        branches=[{"owner": owner, "target_id": f"route-{index}", "state": "registered"} for index, owner in enumerate(owners)],
    )


@pytest.fixture
def request_body(scope: GatewaySearchScopeResolutionDTO) -> FederatedSearchRequest:
    return FederatedSearchRequest.model_validate({
        "scope": {"mode": "resume", "scope": scope.scope},
        "queries": [{"category": "catalog", "query": "needle"}], "max_results": 10,
    })


def page(job: SearchJob, *, cursor: str | None = None) -> WorkspaceSearchResultDTO:
    return WorkspaceSearchResultDTO(
        provider_id="catalog", workspace_id=job.key.owner.workspace_id,
        category=job.key.category, operation=job.key.operation, contract_version=1,
        state="limited" if cursor else "complete", limited_reason="result_limit" if cursor else None,
        catalog=SessionCatalogSearchResultsDTO(revision="r1", catalog_revision=1, cursor=cursor),
    )


async def test_fast_page_precedes_slow_branch_and_terminal_is_unique(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest,
) -> None:
    release = asyncio.Event()
    entered: list[str] = []

    async def query(job: SearchJob) -> WorkspaceSearchResultDTO:
        entered.append(job.key.owner.workspace_id)
        if job.key.owner.workspace_id == "ws-0":
            await release.wait()
        if job.key.owner.workspace_id == "ws-1":
            raise SearchBranchError("target_unavailable", "B 离线", retryable=True)
        return page(job, cursor="owner-cursor")

    events = search_events(plan_search(request_body, scope), scope, request_id="req-1", query=query, budget=NameSearchBudget.create(None))
    first = await anext(events)
    assert first.type == "scope" and entered == []
    received = [first]
    while not any(event.type == "page" for event in received):
        received.append(await anext(events))
    assert received[-1].branch.branch.owner.workspace_id == "ws-2"
    assert not release.is_set()
    # 慢消费不补排队分支，最多三份原页占用内存。
    for _ in range(4):
        await asyncio.sleep(0)
    assert entered == ["ws-0", "ws-1", "ws-2"]
    release.set()
    received.extend([event async for event in events])
    assert [event.sequence for event in received] == list(range(1, len(received) + 1))
    assert {event.request_id for event in received} == {"req-1"}
    assert [event.type for event in received].count("end") == 1 and received[-1].type == "end"
    summary = received[-1].summary
    assert summary.state == "incomplete" and len(summary.branches) == 7
    assert [item.branch.owner.workspace_id for item in summary.branches] == [f"ws-{index}" for index in range(7)]
    assert summary.branches[1].code == "target_unavailable"
    assert len([event for event in received if event.type == "branch"]) == 7


async def test_deadline_includes_queue_and_waits_for_cancelled_owner_cleanup(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest,
) -> None:
    entered: list[str] = []
    cleaned: list[str] = []

    async def query(job: SearchJob) -> WorkspaceSearchResultDTO:
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

    async def query(job: SearchJob) -> WorkspaceSearchResultDTO:
        entered.append(job.key.owner.workspace_id)
        if job.key.owner.workspace_id == "ws-0":
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
    assert settled.is_set() and entered == ["ws-0", "ws-1", "ws-2"]


@pytest.mark.parametrize("limit,expected", [(0, [0] * 7), (1, [1, 0, 0, 0, 0, 0, 0]), (10, [2, 2, 2, 1, 1, 1, 1])])
async def test_allocation_precedes_dispatch_and_zero_never_calls_owner(
    scope: GatewaySearchScopeResolutionDTO, request_body: FederatedSearchRequest, limit: int, expected: list[int],
) -> None:
    jobs = plan_search(request_body.model_copy(update={"max_results": limit}), scope)
    assert [job.request.limit for job in jobs] == expected
    seen = []

    async def query(job: SearchJob) -> WorkspaceSearchResultDTO:
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
    branch["owner"] = {"gateway_id": "gw", "workspace_id": "unknown"}
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

    async def query(job: SearchJob) -> WorkspaceSearchResultDTO:
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

    async def query(job: SearchJob) -> WorkspaceSearchResultDTO:
        result = page(job, cursor="must-not-advance")
        if corruption == "owner":
            result.workspace_id = "another-workspace"
        else:
            result.catalog.items = [SessionCatalogSearchResultDTO.model_validate({
                "node": {"node_id": str(index), "kind": "folder", "name": "needle", "node_revision": 1},
                "relative_path": "needle",
            }) for index in range(job.request.limit + 1)]
        return result

    result = [event async for event in search_events([job], scope, request_id="invalid", query=query, budget=NameSearchBudget.create(None))]
    assert not any(event.type == "page" for event in result)
    assert result[-1].summary.state == "incomplete"
    branch = result[-1].summary.branches[0]
    assert branch.cursor is None and branch.code == "invalid_response"
