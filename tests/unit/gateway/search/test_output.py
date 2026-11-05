"""真实 SSE 编码的整页准入；不把装具领域页称为产品实网验收。"""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from app.gateway.routes.search import _encode_search_sse
from app.gateway.search.output import SearchResponseEncoder
from app.gateway.search.scheduler import (
    SearchBranchError,
    SearchJob,
    SearchPage,
    plan_search,
    search_events,
)
from app.gateway.search.scope import SearchScopeError
from app.schemas.gateway.search import GatewaySearchScopeResolutionDTO
from app.schemas.internal_v2.search import WorkspaceSearchResultDTO
from app.schemas.internal_v2.session_navigation import SessionCatalogSearchResultsDTO
from app.schemas.search_federation import (
    FederatedSearchEventDTO,
    FederatedSearchRequest,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget


@pytest.fixture
def output_scope() -> GatewaySearchScopeResolutionDTO:
    owners = [{"gateway_id": "gateway_0123456789abcdef0123456789abcdef", "workspace_id": f"00000000-0000-4000-8000-{index:012d}"} for index in range(3)]
    return GatewaySearchScopeResolutionDTO.model_validate({
        "scope": {"gateway_id": "gateway_0123456789abcdef0123456789abcdef", "resolved_at": datetime.now(UTC), "mode": "selected", "owners": owners},
        "branches": [{"owner": owner, "state": "registered", "target_id": f"route-{index}"} for index, owner in enumerate(owners)],
    })


@pytest.fixture
def output_jobs(output_scope: GatewaySearchScopeResolutionDTO) -> list[SearchJob]:
    request = FederatedSearchRequest.model_validate({
        "scope": {"mode": "resume", "scope": output_scope.scope}, "max_results": 3,
        "queries": [{"category": "catalog", "query": "needle", "limit": 1}],
        "continuations": [{"branch": {"owner": owner, "category": "catalog"}, "cursor": "原位置\\\"\n🙂" * 50}
                          for owner in output_scope.scope.owners],
    })
    return plan_search(request, output_scope)


@pytest.fixture
def output_page() -> Callable[[SearchJob], SearchPage]:
    def page(job: SearchJob) -> SearchPage:
        return SearchPage(WorkspaceSearchResultDTO(
            provider_id="session-catalog", workspace_id=job.key.owner.workspace_id,
            category="catalog", operation="name", contract_version=1, state="limited", limited_reason="result_limit",
            catalog=SessionCatalogSearchResultsDTO.model_validate({
                "revision": "r1", "catalog_revision": 1, "cursor": "新位置🙂" * 100,
                "items": [{"node": {"node_id": "one", "kind": "folder", "name": "needle🙂" * 600, "node_revision": 1},
                           "relative_path": "needle"}],
            }),
        ))
    return page


async def collect(
    jobs: list[SearchJob], scope: GatewaySearchScopeResolutionDTO, query: Callable[[SearchJob], SearchPage],
) -> tuple[list[FederatedSearchEventDTO], list[bytes]]:
    encoder = SearchResponseEncoder(jobs, scope, "req-output", encode=_encode_search_sse)

    async def execute(job: SearchJob) -> SearchPage:
        return query(job)

    events, frames = [], []
    async for event in search_events(jobs, scope, request_id="req-output", query=execute,
                                    budget=NameSearchBudget.create(None), publish=encoder.publish):
        events.append(event)
        frames.append(encoder.frame)
        data = encoder.frame.decode().split("data: ", 1)[1].strip()
        assert FederatedSearchEventDTO.model_validate_json(data) == event
    assert encoder.used == sum(map(len, frames)) <= encoder.limit
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [event.type for event in events].count("end") == 1
    assert [event.type for event in events].count("branch") == len(jobs)
    return events, frames


async def test_exact_output_limit_and_one_extra_byte_never_skip_unpublished_page(
    output_scope: GatewaySearchScopeResolutionDTO, output_jobs: list[SearchJob],
    output_page: Callable[[SearchJob], SearchPage], monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = output_jobs[:1]
    baseline, frames = await collect(jobs, output_scope, output_page)
    size = sum(map(len, frames))
    monkeypatch.setattr("app.gateway.search.output.MAX_SEARCH_OUTPUT_BYTES", size)
    exact, delivered = await collect(jobs, output_scope, output_page)
    assert exact == baseline and sum(map(len, delivered)) == size
    monkeypatch.setattr("app.gateway.search.output.MAX_SEARCH_OUTPUT_BYTES", size - 1)
    limited, _ = await collect(jobs, output_scope, output_page)
    assert [event.type for event in limited] == ["scope", "branch", "end"]
    summary = limited[-1].summary
    assert summary is not None
    terminal = summary.branches[0]
    assert terminal.state == "limited" and terminal.code == "output_bytes" and terminal.retryable
    assert terminal.cursor == jobs[0].request.cursor
    assert terminal.returned_results == summary.returned_results == 0


async def test_multi_owner_budget_counts_status_cursors_and_encodes_each_page_once(
    output_scope: GatewaySearchScopeResolutionDTO, output_jobs: list[SearchJob],
    output_page: Callable[[SearchJob], SearchPage], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, frames = await collect(output_jobs, output_scope, output_page)
    monkeypatch.setattr("app.gateway.search.output.MAX_SEARCH_OUTPUT_BYTES", sum(map(len, frames)) - 1)
    serialized = []
    original = FederatedSearchEventDTO.model_dump_json

    def observe(self: FederatedSearchEventDTO) -> str:
        if self.type == "page":
            assert self.branch is not None
            serialized.append(self.branch.branch)
        return original(self)

    monkeypatch.setattr(FederatedSearchEventDTO, "model_dump_json", observe)
    result, _ = await collect(output_jobs, output_scope, output_page)
    pages = [event for event in result if event.type == "page"]
    assert len(pages) == 2 and serialized == [job.key for job in output_jobs]
    summary = result[-1].summary
    assert summary is not None
    counts = []
    for event in pages:
        assert event.page is not None
        counts.append(event.page.result_count)
    assert summary.returned_results == sum(counts) == 2
    assert [item.cursor for item in summary.branches] == [
        output_page(job).result.cursor if index < 2 else job.request.cursor for index, job in enumerate(output_jobs)
    ]
    assert summary.branches[-1].code == "output_bytes"


async def test_oversized_owner_error_keeps_room_for_every_terminal(
    output_scope: GatewaySearchScopeResolutionDTO, output_jobs: list[SearchJob], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.gateway.search.output.MAX_SEARCH_OUTPUT_BYTES", 16000)

    def error(job: SearchJob) -> SearchPage:
        raise SearchBranchError("read_failed", "原诊断🙂" * 6000)

    result, _ = await collect(output_jobs, output_scope, error)
    summary = result[-1].summary
    assert summary is not None
    assert summary.state == "incomplete" and summary.returned_results == 0
    assert all(branch.code == "output_bytes" and branch.cursor == job.request.cursor
               for branch, job in zip(summary.branches, output_jobs, strict=True))


def test_scope_and_original_continuations_must_fit_before_dispatch(
    output_scope: GatewaySearchScopeResolutionDTO, output_jobs: list[SearchJob], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.gateway.search.output.MAX_SEARCH_OUTPUT_BYTES", 1024)
    with pytest.raises(SearchScopeError) as failure:
        SearchResponseEncoder(output_jobs, output_scope, "req-output", encode=_encode_search_sse)
    assert failure.value.status_code == 413 and failure.value.code == "output_bytes"
