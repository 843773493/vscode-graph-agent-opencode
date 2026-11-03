"""请求内有界调度；只传输原 owner 的完整页，不实现领域匹配或网络协议。"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from time import monotonic, time
from typing import Literal

from app.core.shielded_cleanup import run_cleanup_shielded
from app.gateway.search.scope import SearchScopeError
from app.schemas.gateway.search import (
    GatewaySearchScopeBranchDTO,
    GatewaySearchScopeResolutionDTO,
)
from app.schemas.internal_v2.search import (
    WorkspaceSearchRequest,
    WorkspaceSearchResultDTO,
)
from app.schemas.search_federation import (
    MAX_SEARCH_BRANCHES,
    MAX_SEARCH_PAGE_BYTES,
    SEARCH_CONCURRENCY,
    FederatedSearchBranchDTO,
    FederatedSearchBranchKeyDTO,
    FederatedSearchEventDTO,
    FederatedSearchQueryDTO,
    FederatedSearchRequest,
    FederatedSearchSummaryDTO,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget


class SearchBranchError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class SearchJob:
    key: FederatedSearchBranchKeyDTO
    scope_branch: GatewaySearchScopeBranchDTO
    request: WorkspaceSearchRequest
    max_bytes: int


SearchPageQuery = Callable[[SearchJob], Awaitable[WorkspaceSearchResultDTO]]


def plan_search(request: FederatedSearchRequest, scope: GatewaySearchScopeResolutionDTO) -> list[SearchJob]:
    """按类别、冻结范围顺序公平预留配额；不先读大页再丢尾部。"""
    queries = {(query.category, query.operation): query for query in request.queries}
    owners = {branch.owner: branch for branch in scope.branches}
    selected = {item.branch: item.cursor for item in request.continuations}
    for key in selected:
        if key.owner not in owners or (key.category, key.operation) not in queries:
            raise SearchScopeError("invalid_continuation", "续查分支不属于原冻结范围或本次查询", status_code=400)
    entries: list[tuple[FederatedSearchBranchKeyDTO, FederatedSearchQueryDTO]] = []
    # 名称类别沿原固定组序；尚未登记的类别也有确定次序，不按响应快慢决定归属顺序。
    for query in sorted(request.queries, key=lambda item: ({"catalog": 0, "thread": 1}.get(item.category, 2), item.category, item.operation)):
        for owner in scope.scope.owners:
            key = FederatedSearchBranchKeyDTO(owner=owner, category=query.category, operation=query.operation)
            if not selected or key in selected:
                entries.append((key, query))
    if len(entries) > MAX_SEARCH_BRANCHES:
        raise SearchScopeError("branch_limit", f"搜索超过 {MAX_SEARCH_BRANCHES} 个分支，请缩小范围或类别")
    per_branch, remainder = divmod(request.max_results, len(entries))
    return [SearchJob(
        key=key, scope_branch=owners[key.owner], max_bytes=MAX_SEARCH_PAGE_BYTES // len(entries),
        request=WorkspaceSearchRequest(
            workspace_id=key.owner.workspace_id, **query.model_dump(exclude={"limit"}),
            limit=min(query.limit, per_branch + int(index < remainder)), cursor=selected.get(key),
            deadline_at=request.deadline_at,
        ),
    ) for index, (key, query) in enumerate(entries)]


async def search_events(
    jobs: list[SearchJob], scope: GatewaySearchScopeResolutionDTO, *,
    request_id: str, query: SearchPageQuery, budget: NameSearchBudget,
) -> AsyncGenerator[FederatedSearchEventDTO, None]:
    """最多并发三个整页；消费暂停时不补新任务，缓冲最多三个已分配页。"""
    sequence = 1
    yield FederatedSearchEventDTO(sequence=sequence, request_id=request_id, type="scope", scope=scope)
    # 期限从 HTTP 准入前确定；即使消费者迟迟不取下一事件，也不能重新给 owner 十五秒。
    pending: dict[asyncio.Task[WorkspaceSearchResultDTO], int] = {}
    statuses: dict[int, FederatedSearchBranchDTO] = {}
    next_index = 0

    def status(
        job: SearchJob, *, state: Literal["complete", "limited", "failed", "cancelled"],
        code: str | None = None, message: str | None = None, retryable: bool = False,
        provider_id: str | None = None, cursor: str | None = None, returned_results: int = 0,
    ) -> FederatedSearchBranchDTO:
        return FederatedSearchBranchDTO(
            branch=job.key, allocated_results=job.request.limit, state=state, code=code, message=message,
            retryable=retryable, provider_id=provider_id, cursor=cursor, returned_results=returned_results,
        )

    async def execute(job: SearchJob) -> WorkspaceSearchResultDTO:
        budget.check()
        return await query(SearchJob(
            key=job.key, scope_branch=job.scope_branch, max_bytes=job.max_bytes,
            request=job.request.model_copy(update={"deadline_at": time() + budget.remaining()}),
        ))

    async def settle() -> None:
        for task in pending:
            if not task.done():
                task.cancel()
        # 所有任务的异常均在本请求结算，不把未消费异常遗留到后台。
        await asyncio.gather(*pending, return_exceptions=True)

    try:
        while next_index < len(jobs) or pending:
            while next_index < len(jobs) and len(pending) < SEARCH_CONCURRENCY:
                index, next_index = next_index, next_index + 1
                job = jobs[index]
                if job.scope_branch.state != "registered":
                    statuses[index] = status(job, state="failed", code=job.scope_branch.code,
                                             message=job.scope_branch.message, retryable=True, cursor=job.request.cursor)
                elif job.request.limit == 0:
                    statuses[index] = status(job, state="limited", code="output_limit", cursor=job.request.cursor)
                elif budget.deadline <= monotonic():
                    statuses[index] = status(job, state="failed", code="deadline", message="搜索总期限已到，分支未分发", retryable=True, cursor=job.request.cursor)
                else:
                    pending[asyncio.create_task(execute(job))] = index
                    continue
                sequence += 1
                yield FederatedSearchEventDTO(sequence=sequence, request_id=request_id, type="branch", branch=statuses[index])
            if not pending:
                continue
            remaining = max(0, budget.deadline - monotonic())
            completed, _ = await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if not completed:
                await settle()
                completed = set(pending)
            for task in sorted(completed, key=lambda item: pending[item]):
                index = pending.pop(task)
                job = jobs[index]
                page: WorkspaceSearchResultDTO | None = None
                try:
                    page = task.result()
                    budget.check()
                    if (page.workspace_id, page.category, page.operation, page.contract_version) != (
                        job.key.owner.workspace_id, job.key.category, job.key.operation, job.request.contract_version,
                    ):
                        raise SearchBranchError("invalid_response", "搜索 Provider 返回了其它 owner／类别／版本的页")
                    domain = page.catalog if page.catalog is not None else page.threads
                    count = len(domain.items) if domain is not None else 0
                    if count > job.request.limit:
                        raise SearchBranchError("invalid_response", "搜索 Provider 返回结果超过分配配额")
                except asyncio.CancelledError:
                    expired = budget.deadline <= monotonic()
                    statuses[index] = status(job, state="failed" if expired else "cancelled",
                                             code="deadline" if expired else "cancelled",
                                             message="搜索总期限已到" if expired else "领域查询已取消", retryable=True, cursor=job.request.cursor)
                except TimeoutError as error:
                    statuses[index] = status(job, state="failed", code="deadline", message=str(error), retryable=True, cursor=job.request.cursor)
                except SearchBranchError as error:
                    statuses[index] = status(job, state="failed", code=error.code, message=str(error),
                                             retryable=error.retryable, cursor=job.request.cursor)
                else:
                    statuses[index] = status(job, state=page.state, provider_id=page.provider_id,
                                             code=page.limited_reason, cursor=domain.cursor if domain is not None else None,
                                             returned_results=count)
                    sequence += 1
                    yield FederatedSearchEventDTO(sequence=sequence, request_id=request_id, type="page", branch=statuses[index], page=page)
                sequence += 1
                yield FederatedSearchEventDTO(sequence=sequence, request_id=request_id, type="branch", branch=statuses[index])
        sequence += 1
        ordered = [statuses[index] for index in range(len(jobs))]
        yield FederatedSearchEventDTO(sequence=sequence, request_id=request_id, type="end", summary=FederatedSearchSummaryDTO(
            state="complete" if all(item.state == "complete" for item in ordered) else "incomplete",
            branches=ordered, returned_results=sum(item.returned_results for item in ordered),
        ))
    finally:
        await run_cleanup_shielded(settle)
