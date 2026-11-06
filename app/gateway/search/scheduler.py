"""请求内有界调度；只传输原 owner 的完整页，不实现领域匹配或网络协议。"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic, time
from typing import Literal

from app.core.resource_uri import parse_resource_uri
from app.core.shielded_cleanup import run_cleanup_shielded
from app.gateway.search.scope import SearchScopeError
from app.schemas.gateway.diagnostics import (
    GatewayDiagnosticLogQueryRequest,
    GatewayDiagnosticLogQueryResultDTO,
)
from app.schemas.gateway.search import (
    GatewaySearchScopeBranchDTO,
    GatewaySearchScopeResolutionDTO,
)
from app.schemas.internal_v2.search import (
    WorkspaceFileSearchTargetDTO,
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
    FederatedSearchSourceDTO,
    FederatedSearchSummaryDTO,
)
from app.services.business.session_context_resource import (
    parse_session_context_ref,
    session_resource_identity,
    workspace_session_inventory_identity,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget

logger = logging.getLogger(__name__)


class SearchBranchError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, provider_id: str | None = None,
                 error_owner: str | None = None, reason: str | None = None, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.provider_id = provider_id
        self.error_owner = error_owner
        self.reason = reason
        self.http_status = http_status


@dataclass(frozen=True)
class SearchJob:
    key: FederatedSearchBranchKeyDTO
    scope_branch: GatewaySearchScopeBranchDTO | None
    request: WorkspaceSearchRequest | GatewayDiagnosticLogQueryRequest
    max_bytes: int
    allow_catalog_mirror: bool = False


@dataclass(frozen=True)
class SearchPage:
    result: WorkspaceSearchResultDTO | GatewayDiagnosticLogQueryResultDTO
    source: FederatedSearchSourceDTO = field(default_factory=FederatedSearchSourceDTO)


SearchPageQuery = Callable[[SearchJob], Awaitable[SearchPage]]


def output_limited(job: SearchJob) -> FederatedSearchBranchDTO:
    """未交付整页时保留输入续点，不能跳过本页的任何命中。"""
    return FederatedSearchBranchDTO(
        branch=job.key, state="limited", code="output_bytes",
        message="搜索流输出达到字节上限，请缩小范围或页大小后继续原分支",
        retryable=True, cursor=job.request.cursor, allocated_results=job.request.limit,
    )


def plan_search(request: FederatedSearchRequest, scope: GatewaySearchScopeResolutionDTO) -> list[SearchJob]:
    """按类别、冻结范围顺序公平预留配额；不先读大页再丢尾部。"""
    owners = {branch.owner: branch for branch in scope.branches}
    selected = {item.branch: item.cursor for item in request.continuations}
    entries: list[tuple[FederatedSearchBranchKeyDTO, FederatedSearchQueryDTO, WorkspaceFileSearchTargetDTO | None]] = []
    # 名称类别沿原固定组序；尚未登记的类别也有确定次序，不按响应快慢决定归属顺序。
    for query in sorted(request.queries, key=lambda item: ({"catalog": 0, "thread": 1}.get(item.category, 2), item.category, item.operation)):
        if query.category == "runtime_log":
            for target in query.diagnostic_targets:
                selected_owners = [owner for owner in scope.scope.owners if owner.gateway_id == target.gateway_id]
                if not selected_owners or any(workspace_id not in {owner.workspace_id for owner in selected_owners}
                                              for workspace_id in target.scope.workspace_ids):
                    raise SearchScopeError("owner_mismatch", "诊断来源不属于本次冻结 Gateway／工作区集合", status_code=400)
                entries.append((FederatedSearchBranchKeyDTO(diagnostics=target, category=query.category,
                                                           operation=query.operation), query, None))
            continue
        if any(target.owner not in owners for target in query.file_targets):
            raise SearchScopeError("owner_mismatch", "文件根不属于本次冻结工作区集合", status_code=400)
        for owner in scope.scope.owners:
            targets = [item.target for item in query.file_targets if item.owner == owner] if query.file_targets else [None]
            for target in targets:
                key = FederatedSearchBranchKeyDTO(owner=owner, category=query.category, operation=query.operation,
                                                 resource_uri=target.uri if target else None)
                entries.append((key, query, target))
    keys = {key for key, _, _ in entries}
    if any(key not in keys for key in selected):
        raise SearchScopeError("invalid_continuation", "续查分支不属于原冻结范围、查询或目录根", status_code=400)
    if selected:
        entries = [entry for entry in entries if entry[0] in selected]
    if len(entries) > MAX_SEARCH_BRANCHES:
        raise SearchScopeError("branch_limit", f"搜索超过 {MAX_SEARCH_BRANCHES} 个分支，请缩小范围或类别")
    per_branch, remainder = divmod(request.max_results, len(entries))
    jobs: list[SearchJob] = []
    for index, (key, query, target) in enumerate(entries):
        limit = min(query.limit, per_branch + int(index < remainder))
        if key.diagnostics is not None:
            owner_request: WorkspaceSearchRequest | GatewayDiagnosticLogQueryRequest = GatewayDiagnosticLogQueryRequest(
                scope=key.diagnostics.scope, query=query.query, limit=min(100, limit),
                cursor=selected.get(key), deadline_at=request.deadline_at,
            )
        else:
            assert key.owner is not None
            owner_request = WorkspaceSearchRequest(
                workspace_id=key.owner.workspace_id,
                **query.model_dump(exclude={"limit", "file_targets", "allow_catalog_mirror", "diagnostic_targets"}),
                file=target, limit=limit, cursor=selected.get(key), deadline_at=request.deadline_at,
            )
        jobs.append(SearchJob(key=key, scope_branch=owners[key.owner] if key.owner is not None else None,
                              max_bytes=MAX_SEARCH_PAGE_BYTES // len(entries), request=owner_request,
                              allow_catalog_mirror=query.allow_catalog_mirror))
    return jobs


async def search_events(
    jobs: list[SearchJob], scope: GatewaySearchScopeResolutionDTO, *,
    request_id: str, query: SearchPageQuery, budget: NameSearchBudget,
    publish: Callable[[FederatedSearchEventDTO], bool] = lambda _: True,
) -> AsyncGenerator[FederatedSearchEventDTO, None]:
    """最多并发三个整页；消费暂停时不补新任务，缓冲最多三个已分配页。"""
    queued_at = monotonic()
    sequence = 1
    event = FederatedSearchEventDTO(sequence=sequence, request_id=request_id, type="scope", scope=scope)
    if not publish(event):
        raise RuntimeError("搜索流未预留范围事件的输出预算")
    yield event
    # 期限从 HTTP 准入前确定；即使消费者迟迟不取下一事件，也不能重新给 owner 十五秒。
    pending: dict[asyncio.Task[SearchPage], int] = {}
    statuses: dict[int, FederatedSearchBranchDTO] = {}
    next_index = 0

    def status(
        job: SearchJob, *, state: Literal["complete", "limited", "failed", "cancelled"],
        code: str | None = None, message: str | None = None, retryable: bool = False,
        provider_id: str | None = None, cursor: str | None = None, returned_results: int = 0,
        error_owner: str | None = None, reason: str | None = None, http_status: int | None = None,
    ) -> FederatedSearchBranchDTO:
        return FederatedSearchBranchDTO(
            branch=job.key, allocated_results=job.request.limit, state=state, code=code, message=message,
            retryable=retryable, provider_id=provider_id, cursor=cursor, returned_results=returned_results,
            error_owner=error_owner, reason=reason, http_status=http_status,
        )

    def terminal(index: int) -> FederatedSearchEventDTO:
        event = FederatedSearchEventDTO(sequence=sequence, request_id=request_id, type="branch", branch=statuses[index])
        if not publish(event):
            statuses[index] = output_limited(jobs[index])
            event = event.model_copy(update={"branch": statuses[index]})
            if not publish(event):
                raise RuntimeError("搜索流未预留分支终态的输出预算")
        return event

    async def execute(job: SearchJob) -> SearchPage:
        budget.check()
        started_at = monotonic()
        reply: SearchPage | None = None
        try:
            reply = await query(SearchJob(
                key=job.key, scope_branch=job.scope_branch, max_bytes=job.max_bytes,
                allow_catalog_mirror=job.allow_catalog_mirror,
                request=job.request.model_copy(update={"deadline_at": time() + budget.remaining()}),
            ))
            return reply
        finally:
            if logger.isEnabledFor(logging.DEBUG):
                # 接收量不是最终交付量；未取得领域页时保留空值，失败和取消仍沿原出口。
                logger.debug(
                    "搜索分支读取 request_id=%(request_id)s branch=%(branch)s queue_ms=%(queue_ms).3f "
                    "provider_ms=%(provider_ms).3f provider_id=%(provider_id)s state=%(state)s "
                    "received_results=%(received_results)s source=%(source)s stale=%(stale)s",
                    {"request_id": request_id, "branch": job.key, "queue_ms": (started_at - queued_at) * 1000,
                     "provider_ms": (monotonic() - started_at) * 1000,
                     "provider_id": reply.result.provider_id if reply is not None else None,
                     "state": reply.result.state if reply is not None else None,
                     "received_results": reply.result.result_count if reply is not None else None,
                     "source": reply.source.kind if reply is not None else None,
                     "stale": reply.source.stale if reply is not None else None},
                )

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
                if job.scope_branch is not None and job.scope_branch.state != "registered":
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
                yield terminal(index)
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
                page: WorkspaceSearchResultDTO | GatewayDiagnosticLogQueryResultDTO | None = None
                try:
                    reply = task.result()
                    page = reply.result
                    budget.check()
                    if isinstance(page, GatewayDiagnosticLogQueryResultDTO):
                        target = job.key.diagnostics
                        if target is None or page.gateway_id != target.gateway_id or page.scope != target.scope or (
                            page.query != job.request.query or any(item.reference.gateway_id != target.gateway_id
                            or item.source.source != target.scope.source or (target.scope.source == "workspace"
                            and not set(item.source.workspace_ids).intersection(target.scope.workspace_ids)) for item in page.items)
                        ):
                            raise SearchBranchError("invalid_response", "运行日志搜索返回了其它 diagnostics 来源的页")
                    elif job.key.owner is None or not isinstance(job.request, WorkspaceSearchRequest) or (
                        page.workspace_id, page.category, page.operation, page.contract_version
                    ) != (job.key.owner.workspace_id, job.key.category, job.key.operation, job.request.contract_version):
                        raise SearchBranchError("invalid_response", "搜索 Provider 返回了其它 owner／类别／版本的页")
                    if isinstance(page, WorkspaceSearchResultDTO) and page.context is not None:
                        assert job.key.owner is not None
                        context = page.context
                        references = [(context, "workspace_sessions"), *((match, "session") for match in context.matches)]
                        for reference, kind in references:
                            resource = parse_session_context_ref(resource_identity=reference.resource_identity,
                                uri=reference.uri, scope=reference.scope, local_gateway_id=job.key.owner.gateway_id)
                            if resource.scope != "workspace" or resource.scope_id != job.key.owner.workspace_id or (
                                resource.gateway_authority is not None or resource.kind != kind
                            ):
                                raise SearchBranchError("invalid_response", "上下文搜索返回了其它资源或 owner 的身份")
                            if kind == "workspace_sessions":
                                expected_identity = workspace_session_inventory_identity(job.key.owner.workspace_id)
                            else:
                                assert resource.session_id is not None
                                expected_identity = session_resource_identity(job.key.owner.workspace_id, resource.session_id)
                            if resource.resource_identity != expected_identity:
                                raise SearchBranchError("invalid_response", "上下文搜索的资源身份与规范 URI 不一致")
                    count = page.result_count
                    if (isinstance(page, WorkspaceSearchResultDTO) and isinstance(job.request, WorkspaceSearchRequest)
                        and job.key.owner is not None and page.files is not None and job.request.file is not None and job.request.file.uri is not None
                        and page.files.node.uri != parse_resource_uri(job.request.file.uri, local_gateway_id=job.key.owner.gateway_id).display_uri):
                        raise SearchBranchError("invalid_response", "文件搜索返回了其它目录根的页")
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
                                             retryable=error.retryable, provider_id=error.provider_id, cursor=job.request.cursor,
                                             error_owner=error.error_owner, reason=error.reason, http_status=error.http_status)
                else:
                    statuses[index] = status(job, state=page.state, provider_id=page.provider_id,
                                             code=page.limited_reason, cursor=page.cursor,
                                             returned_results=count)
                    event = FederatedSearchEventDTO(sequence=sequence + 1, request_id=request_id, type="page", branch=statuses[index],
                        page=page if isinstance(page, WorkspaceSearchResultDTO) else None,
                        diagnostic_logs=page if isinstance(page, GatewayDiagnosticLogQueryResultDTO) else None, source=reply.source)
                    if publish(event):
                        sequence += 1
                        yield event
                    else:
                        statuses[index] = output_limited(job)
                sequence += 1
                yield terminal(index)
        sequence += 1
        ordered = [statuses[index] for index in range(len(jobs))]
        event = FederatedSearchEventDTO(sequence=sequence, request_id=request_id, type="end", summary=FederatedSearchSummaryDTO(
            state="complete" if all(item.state == "complete" for item in ordered) else "incomplete",
            branches=ordered, returned_results=sum(item.returned_results for item in ordered),
        ))
        if not publish(event):
            raise RuntimeError("搜索流未预留整体终态的输出预算")
        yield event
    finally:
        await run_cleanup_shielded(settle)
