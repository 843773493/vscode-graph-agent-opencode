"""经原登记路由读取领域页；请求取消与路由失效都须结算后释放引用。"""

import asyncio
import json
import logging
from time import time

import httpx
from pydantic import ValidationError

from app.core.failures import report_failure
from app.core.shielded_cleanup import run_cleanup_shielded
from app.gateway.control.catalog_search import GatewaySessionCatalogSearchService
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.registry.owners import workspace_owner
from app.gateway.search.scheduler import SearchBranchError, SearchJob, SearchPage
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.search import WorkspaceSearchResultDTO
from app.schemas.search_federation import (
    MAX_SEARCH_ERROR_CHARS,
    FederatedSearchSourceDTO,
)
from app.services.business.session_navigation.catalog_query import CatalogCursorError

logger = logging.getLogger(__name__)

class SearchTransport:
    def __init__(self, registry: GatewayWorkspaceRegistry, client: httpx.AsyncClient, *, gateway_id: str, request_id: str,
                 catalog_mirror: GatewaySessionCatalogSearchService | None = None) -> None:
        self.registry = registry
        self.client = client
        self.gateway_id = gateway_id
        self.request_id = request_id
        self.catalog_mirror = catalog_mirror

    async def query(self, job: SearchJob) -> SearchPage:
        target_id = job.scope_branch.target_id
        if target_id is None or not self.registry.has_target(target_id):
            raise SearchBranchError("owner_unavailable", "搜索目标已移除", retryable=True)
        target = self.registry.resolve(target_id)
        if workspace_owner(target, self.registry, self.gateway_id) != job.key.owner:
            raise SearchBranchError("owner_unavailable", "登记路由的稳定归属已经改变", retryable=True)
        try:
            return SearchPage(await self._query_owner(job))
        except SearchBranchError as error:
            if error.code != "target_unavailable" or not job.allow_catalog_mirror:
                raise
            return self._query_mirror(job, error)

    def _query_mirror(self, job: SearchJob, unavailable: SearchBranchError) -> SearchPage:
        if self.catalog_mirror is None:
            raise unavailable
        try:
            page, updated_at = self.catalog_mirror.search_mirror(
                job.key.owner, query=job.request.query, limit=job.request.limit,
                cursor=job.request.cursor, deadline_at=job.request.deadline_at,
            )
        except LookupError as error:
            raise SearchBranchError("target_unavailable", f"{unavailable}；{error}", retryable=True) from error
        except CatalogCursorError as error:
            raise SearchBranchError(error.code, str(error), retryable=True,
                                    error_owner="session_catalog", reason=error.reason) from error
        except (ValueError, TypeError) as error:
            raise SearchBranchError("owner_rejected", str(error)) from error
        result = WorkspaceSearchResultDTO(
            provider_id="session-catalog", workspace_id=job.key.owner.workspace_id,
            category="catalog", operation="name", contract_version=1,
            state="limited" if page.cursor else "complete", limited_reason="result_limit" if page.cursor else None,
            catalog=page,
        )
        # 镜像没有网络接收边界，在此限制完整领域页，不截断后推进原 cursor。
        if len(result.model_dump_json().encode()) > job.max_bytes:
            raise SearchBranchError("delivery_limit", "目录镜像页超过本次字节配额，请缩小页大小", retryable=True)
        return SearchPage(result, FederatedSearchSourceDTO(
            kind="gateway_catalog_mirror", updated_at=updated_at, stale=True, unavailable_reason=str(unavailable),
        ))

    async def _query_owner(self, job: SearchJob) -> WorkspaceSearchResultDTO:
        target_id = job.scope_branch.target_id
        assert target_id is not None
        target = self.registry.resolve(target_id)
        try:
            url = self.registry.resolve_service_url(target_id, "workspace_api")
        except LookupError as error:
            raise SearchBranchError("target_unavailable", str(error), retryable=True) from error
        headers = self.registry._target_headers(target)
        lease = self.registry.acquire_route_reference(target_id, streaming=False)
        operation = asyncio.create_task(self._read(job, url, headers))
        invalidated = asyncio.create_task(lease.invalidated.wait())

        async def settle() -> None:
            for task in (operation, invalidated):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, invalidated, return_exceptions=True)

        try:
            await asyncio.wait((operation, invalidated), return_when=asyncio.FIRST_COMPLETED)
            if lease.invalidated.is_set():
                raise SearchBranchError("route_invalidated", "搜索期间目标路由失效，请重试原分支", retryable=True)
            return operation.result()
        finally:
            try:
                await run_cleanup_shielded(settle)
            finally:
                self.registry.release_route_reference(target_id, streaming=False)

    async def _read(self, job: SearchJob, url: str, headers: dict[str, str]) -> WorkspaceSearchResultDTO:
        headers = {**headers, "X-Request-ID": self.request_id, "X-BoxTeam-Gateway-Id": self.gateway_id}
        deadline = job.request.deadline_at
        assert deadline is not None
        try:
            async with self.client.stream(
                "POST", f"{url.rstrip('/')}/api/v1/search", headers=headers,
                json=job.request.model_dump(mode="json"), timeout=max(0.001, deadline - time()),
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > job.max_bytes:
                        raise SearchBranchError("delivery_limit", "领域响应超过本次字节配额，请缩小页大小后重试原页", retryable=True)
                    body.extend(chunk)
                if response.headers.get("X-Request-ID") != self.request_id:
                    raise SearchBranchError("invalid_response", "领域响应丢失本次 request_id")
                if response.status_code != 200:
                    self._raise_owner_error(response.status_code, body)
                envelope = APIResponse[WorkspaceSearchResultDTO].model_validate_json(body)
                if envelope.code != 0 or envelope.request_id != self.request_id or envelope.data is None:
                    raise SearchBranchError("invalid_response", "领域响应的 request_id 或数据不符")
                return envelope.data
        except httpx.TimeoutException as error:
            raise SearchBranchError("deadline", f"领域查询超时: {type(error).__name__}: {error}", retryable=True,
                                    error_owner="search_transport", reason=type(error).__name__) from error
        except httpx.RequestError as error:
            raise SearchBranchError("target_unavailable", f"领域请求失败: {error}", retryable=True,
                                    error_owner="search_transport", reason=type(error).__name__) from error
        except ValidationError as error:
            detail = str(error.errors(include_input=False, include_url=False))[:MAX_SEARCH_ERROR_CHARS]
            message = report_failure(error, diagnostic_logger=logger, detail=(
                f"领域响应不符合公开合同: workspace_id={job.key.owner.workspace_id}, request_id={self.request_id}; {detail}"
            ))
            raise SearchBranchError("invalid_response", message) from error

    @staticmethod
    def _raise_owner_error(status: int, body: bytearray) -> None:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = None
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict):
            code, message = detail.get("code"), detail.get("message")
        else:
            code, message = "owner_http_error", detail
        if not isinstance(code, str) or not isinstance(message, str):
            code, message = "owner_http_error", f"领域请求返回 HTTP {status}，响应没有合法错误详情"
        if len(message) > MAX_SEARCH_ERROR_CHARS:
            message = message[:MAX_SEARCH_ERROR_CHARS] + "…（详情超过搜索响应上限，请按 request_id 查看原诊断）"
        provider_id = detail.get("provider_id") if isinstance(detail, dict) and detail.get("owner") == "search_provider" else None
        owner = detail.get("owner") if isinstance(detail, dict) else None
        reason = detail.get("reason") if isinstance(detail, dict) else None
        raise SearchBranchError(code, message, retryable=status >= 500 or code in {
                                    "deadline", "owner_rejected", "invalid_catalog_cursor", "stale_catalog_cursor",
                                } or (owner == "resource_file_operations" and reason in {"invalid_cursor", "stale_cursor"}),
                                provider_id=provider_id if isinstance(provider_id, str) else None,
                                error_owner=owner if isinstance(owner, str) else None,
                                reason=reason if isinstance(reason, str) else None, http_status=status)
