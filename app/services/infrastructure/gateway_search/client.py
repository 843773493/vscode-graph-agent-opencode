"""复用 Gateway HTTP 连接和官方 SSE 解码；不分发工作区请求。"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing

import httpx
from httpx_sse import EventSource

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.schemas.gateway.search import (
    GatewaySearchScopeDTO,
    GatewayWorkspaceDiscoveryResultsDTO,
)
from app.schemas.search_federation import (
    FederatedSearchBranchDTO,
    FederatedSearchBranchKeyDTO,
    FederatedSearchEventDTO,
    FederatedSearchRequest,
)
from app.services.business.session_navigation.name_search_budget import (
    NAME_SEARCH_MAX_SECONDS,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection


class IncompleteSearchStreamError(RuntimeError):
    """正常读取结束但缺少最终汇总；保留原请求身份及续查提示。"""

    code = "incomplete_stream"

    def __init__(self, request_id: str) -> None:
        super().__init__("Gateway 搜索流在最终汇总前断开；已交付分支须按原范围独立续查")
        self.request_id = request_id


class GatewaySearchEventValidator:
    """SSE 与 JSON relay 共用事件顺序、目标归属和唯一终态合同。"""

    def __init__(self, request: FederatedSearchRequest, *, request_id: str) -> None:
        self.request = request
        self.request_id = request_id
        self.scope: GatewaySearchScopeDTO | None = None
        self.terminals: dict[FederatedSearchBranchKeyDTO, FederatedSearchBranchDTO] = {}
        self.pages: set[FederatedSearchBranchKeyDTO] = set()
        self.ended = False
        self.sequence = 0
        self.diagnostic_branches: set[FederatedSearchBranchKeyDTO] = set()

    def accept(self, event: FederatedSearchEventDTO) -> None:
        self.sequence += 1
        if self.ended or event.sequence != self.sequence or event.request_id != self.request_id:
            raise RuntimeError("Gateway 搜索事件顺序、request_id 或唯一终态无效")
        if event.type == "scope":
            if self.sequence != 1 or event.scope is None:
                raise RuntimeError("Gateway 搜索范围必须先于领域页且只发布一次")
            self.scope = event.scope.scope
            selection = self.request.scope
            expected = ((selection.origin,) if selection.mode == "origin" else selection.owners)
            if (selection.mode in {"origin", "selected"} and set(self.scope.owners) != set(expected)) or (
                selection.mode == "resume" and self.scope != selection.scope
            ):
                raise RuntimeError("Gateway 搜索实际范围与明确选择不一致")
            for query in self.request.queries:
                for diagnostic in query.diagnostic_targets:
                    workspaces = {owner.workspace_id for owner in self.scope.owners if owner.gateway_id == diagnostic.gateway_id}
                    if not workspaces or not set(diagnostic.scope.workspace_ids).issubset(workspaces):
                        raise RuntimeError("Gateway diagnostics 来源不属于冻结范围")
                    self.diagnostic_branches.add(FederatedSearchBranchKeyDTO(
                        diagnostics=diagnostic, category=query.category, operation=query.operation,
                    ))
            if self.request.continuations:
                self.diagnostic_branches.intersection_update(item.branch for item in self.request.continuations)
        elif self.scope is None:
            raise RuntimeError("Gateway 搜索缺少实际范围")
        elif event.branch is not None:
            branch = event.branch.branch
            diagnostic = branch.diagnostics
            selected_diagnostic = branch in self.diagnostic_branches
            if (not selected_diagnostic if diagnostic is not None else branch.owner not in self.scope.owners) or branch in self.terminals:
                raise RuntimeError("Gateway 搜索分支归属错误或终态后继续发布")
            if event.type == "page":
                if diagnostic is not None:
                    logs = event.diagnostic_logs
                    query = next(query for query in self.request.queries if diagnostic in query.diagnostic_targets)
                    valid = (logs is not None and logs.gateway_id == diagnostic.gateway_id and logs.scope == diagnostic.scope
                             and logs.query == query.query and logs.result_count <= event.branch.allocated_results)
                    count = logs.result_count if logs is not None else -1
                else:
                    page = event.page
                    valid = page is not None and branch.owner is not None and (page.workspace_id, page.category, page.operation) == (
                        branch.owner.workspace_id, branch.category, branch.operation,
                    )
                    count = page.result_count if page is not None else -1
                if not valid or branch in self.pages or event.branch.returned_results != count:
                    raise RuntimeError("Gateway 搜索领域页归属、计数或重复发布无效")
                self.pages.add(branch)
            else:
                if diagnostic is not None and event.branch.returned_results and branch not in self.pages:
                    raise RuntimeError("Gateway 日志分支没有交付对应正文页")
                self.terminals[branch] = event.branch
        elif event.summary is not None:
            summary = event.summary
            if {item.branch: item for item in summary.branches} != self.terminals or (
                len(summary.branches) != len(self.terminals)
                or summary.returned_results != sum(item.returned_results for item in self.terminals.values())
                or not self.diagnostic_branches.issubset(self.terminals)
            ):
                raise RuntimeError("Gateway 搜索汇总与已结算分支不一致")
            self.ended = True

    def finish(self) -> None:
        if not self.ended:
            raise IncompleteSearchStreamError(self.request_id)


class GatewayResourceSearchClient:
    def __init__(
        self, *, http_client: httpx.AsyncClient, connection: GatewayTransportConnection,
        local_token_supplier: Callable[[], str],
    ) -> None:
        self._http = http_client
        self._connection = connection
        self._local_token_supplier = local_token_supplier

    def _headers(self, request_id: str) -> dict[str, str]:
        if not request_id:
            raise ValueError("Gateway 搜索需要发起者提供 request_id")
        return {"X-Local-Token": self._local_token_supplier(), "X-Request-ID": request_id}

    @staticmethod
    async def _check_response(response: httpx.Response, request_id: str) -> None:
        if response.headers.get("X-Request-ID") != request_id:
            raise RuntimeError(f"Gateway 搜索响应 request_id 漂移: path={response.request.url.path}, status={response.status_code}, request_id={request_id}")
        if not response.is_success:
            await response.aread()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("request_id") != request_id:
                raise RuntimeError(f"Gateway 搜索错误响应信封无效: path={response.request.url.path}, status={response.status_code}, request_id={request_id}")
            # Gateway 的具名搜索拒绝保留原原因；不投影未知异常或任意错误正文。
            detail = payload.get("detail")
            reason = f"code={detail.get('code')}; {detail.get('message', '')}" if isinstance(detail, dict) else ""
            raise WorkspaceSessionContextAccessError(
                f"Gateway 搜索失败: status={response.status_code}, request_id={request_id}; {reason}"
            )

    async def discover_workspaces(
        self, *, query: str = "", limit: int = 50, cursor: str | None = None,
        include_folders: bool = False, request_id: str,
    ) -> GatewayWorkspaceDiscoveryResultsDTO:
        params: dict[str, str | int | bool] = {"query": query, "limit": limit, "include_folders": include_folders}
        if cursor is not None:
            params["cursor"] = cursor
        response = await self._http.get(
            self._connection.resolve_gateway_url() + "/api/gateway/search/workspaces",
            params=params, headers=self._headers(request_id), timeout=self._connection.resolve_timeout_seconds(),
        )
        await self._check_response(response, request_id)
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("request_id") != request_id:
            raise RuntimeError("Gateway 工作区发现响应信封无效")
        return GatewayWorkspaceDiscoveryResultsDTO.model_validate(payload["data"])

    async def search(
        self, request: FederatedSearchRequest, *, request_id: str,
    ) -> AsyncIterator[FederatedSearchEventDTO]:
        # 建连、全部事件和关闭共用一个总期限；没有逐工作区重试或续期。
        deadline = min(time.time() + NAME_SEARCH_MAX_SECONDS, request.deadline_at if request.deadline_at is not None else float("inf"))
        deadline = min(deadline, time.time() + self._connection.resolve_timeout_seconds())
        request = request.model_copy(update={"deadline_at": deadline})
        if deadline <= time.time():
            raise TimeoutError("Gateway 搜索期限已到，不发起请求")
        headers = {**self._headers(request_id), "Accept": "text/event-stream"}
        lifecycle = GatewaySearchEventValidator(request, request_id=request_id)
        async with asyncio.timeout(max(0, deadline - time.time())):
            async with self._http.stream(
                "POST", self._connection.resolve_gateway_url() + "/api/gateway/search/agent",
                json=request.model_dump(mode="json"), headers=headers, timeout=max(0, deadline - time.time()),
            ) as response:
                await self._check_response(response, request_id)
                async with aclosing(EventSource(response).aiter_sse()) as frames:
                    async for frame in frames:
                        # 解码器沿 SSE 合同保留 last-event-id，注释心跳可形成无 data 的 message。
                        if frame.event == "message" and not frame.data:
                            continue
                        if frame.event != "search":
                            raise RuntimeError(f"Gateway 搜索事件类型无效: {frame.event!r}")
                        event = FederatedSearchEventDTO.model_validate_json(frame.data)
                        if frame.id != str(event.sequence):
                            raise RuntimeError("Gateway 搜索 SSE id 与事件序号不一致")
                        lifecycle.accept(event)
                        yield event
        lifecycle.finish()
