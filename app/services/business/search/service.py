"""工作区唯一搜索分派；不拥有领域数据或跨目标队列。"""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Coroutine
from time import time

from app.core.failures import failure_detail
from app.core.shielded_cleanup import run_cleanup_shielded
from app.schemas.internal_v2.search import (
    SearchProviderCapabilityDTO,
    WorkspaceSearchCapabilitiesDTO,
    WorkspaceSearchRequest,
    WorkspaceSearchResultDTO,
)
from app.services.business.session_navigation.name_search_budget import NameSearchBudget

SearchQuery = Callable[[WorkspaceSearchRequest, str | None], Coroutine[None, None, WorkspaceSearchResultDTO]]


class SearchContractError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class SearchProviderReadError(RuntimeError):
    """只有已分派 Provider 的读取错误带其归属；原异常和完整诊断仍保留。"""

    def __init__(self, capability: SearchProviderCapabilityDTO, operation: str, error: OSError | sqlite3.Error) -> None:
        self.provider_id = capability.provider_id
        self.category = capability.category
        self.operation = operation
        detail = failure_detail(error, stage=f"search:{self.provider_id}/{operation}", label="搜索 Provider 读取失败")
        if isinstance(error, OSError) and error.errno is not None and error.filename is not None:
            detail += f", reason={error.strerror}, filename={error.filename!r}"
            if error.filename2 is not None:
                detail += f", filename2={error.filename2!r}"
        super().__init__(detail)

    def detail(self) -> dict[str, str]:
        return {"owner": "search_provider", "code": "provider_read_failed", "provider_id": self.provider_id,
                "category": self.category, "operation": self.operation, "message": str(self)}


class SearchRegistration:
    def __init__(
        self, capability: SearchProviderCapabilityDTO, query: SearchQuery,
        release: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self.capability = capability
        self.query = query
        self.active = True
        self.tasks: set[asyncio.Task[WorkspaceSearchResultDTO]] = set()
        self._release = release
        self._closing: asyncio.Task[None] | None = None

    def close_admission(self) -> None:
        if self._closing is None:
            self.active = False
            for task in self.tasks:
                if not task.cancelling():
                    task.cancel()
            self._closing = asyncio.create_task(self._settle())

    async def aclose(self) -> None:
        self.close_admission()
        closing = self._closing
        assert closing is not None
        await run_cleanup_shielded(lambda: closing)

    async def _settle(self) -> None:
        # 查询错误仍由原请求消费；释放须等待所有原 owner 退出安全边界。
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self._release is not None:
            await self._release()


class WorkspaceSearchService:
    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self._providers: dict[tuple[str, str], SearchRegistration] = {}
        self._closed = False

    def register(
        self, capability: SearchProviderCapabilityDTO, query: SearchQuery,
        *, release: Callable[[], Awaitable[None]] | None = None,
    ) -> SearchRegistration:
        if self._closed:
            raise SearchContractError("provider_unavailable", "工作区搜索服务已关闭")
        if len(set(capability.operations)) != len(capability.operations):
            raise ValueError(f"搜索 Provider 重复声明操作: {capability.provider_id}")
        keys = tuple((capability.category, operation) for operation in capability.operations)
        for key in keys:
            if key in self._providers:
                raise ValueError(f"搜索类别／操作已注册: {key}")
        if any(item.capability.provider_id == capability.provider_id for item in self._providers.values()):
            raise ValueError(f"搜索 Provider 身份已注册: {capability.provider_id}")

        async def unregister() -> None:
            if release is not None:
                await release()
            for key in keys:
                del self._providers[key]

        registration = SearchRegistration(capability, query, unregister)
        self._providers.update(dict.fromkeys(keys, registration))
        return registration

    def capabilities(self) -> WorkspaceSearchCapabilitiesDTO:
        return WorkspaceSearchCapabilitiesDTO(
            workspace_id=self.workspace_id,
            providers=[item.capability for item in dict.fromkeys(self._providers.values()) if item.active],
        )

    async def search(
        self, request: WorkspaceSearchRequest, *, gateway_id: str | None = None,
    ) -> WorkspaceSearchResultDTO:
        if request.workspace_id != self.workspace_id:
            raise SearchContractError("owner_mismatch", "搜索请求与目标工作区归属不一致")
        registration = self._providers.get((request.category, request.operation))
        if self._closed or registration is None or not registration.active:
            raise SearchContractError("provider_unavailable", f"搜索能力不可用: {request.category}/{request.operation}")
        capability = registration.capability
        if request.contract_version != capability.contract_version:
            raise SearchContractError("unsupported_version", f"搜索合同版本不匹配: {capability.provider_id}")
        if request.match_mode not in capability.match_modes:
            raise SearchContractError("unsupported_option", f"搜索 Provider 不支持匹配模式: {request.match_mode}")
        if not request.query or (request.match_mode == "fuzzy" and not request.query.strip()):
            raise SearchContractError("invalid_query", "搜索词不能为空")
        if request.limit > capability.max_results:
            raise SearchContractError("invalid_query", f"搜索输出上限超过 Provider 限制: {capability.max_results}")
        budget = NameSearchBudget.create(request.deadline_at)
        if request.limit == 0:
            return WorkspaceSearchResultDTO(
                provider_id=capability.provider_id, workspace_id=self.workspace_id,
                category=request.category, operation=request.operation, contract_version=capability.contract_version,
                state="limited", limited_reason="output_limit",
            )
        request = request.model_copy(update={"deadline_at": time() + budget.remaining()})
        task = asyncio.create_task(registration.query(request, gateway_id))
        registration.tasks.add(task)
        try:
            result = await asyncio.shield(task)
            if not registration.active:
                raise SearchContractError("provider_unavailable", f"搜索 Provider 已注销: {capability.provider_id}")
            return result
        except asyncio.CancelledError:
            if not task.cancelling():
                task.cancel()

            async def settle() -> None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            await run_cleanup_shielded(settle)
            if not registration.active:
                raise SearchContractError("provider_unavailable", f"搜索 Provider 已注销: {capability.provider_id}") from None
            raise
        except TimeoutError:
            # TimeoutError 也是 OSError；原期限不能被改标为读取失败。
            raise
        except (OSError, sqlite3.Error) as error:
            raise SearchProviderReadError(capability, request.operation, error) from error
        finally:
            registration.tasks.discard(task)

    async def aclose(self) -> None:
        self._closed = True
        registrations = tuple(dict.fromkeys(self._providers.values()))
        for registration in registrations:
            registration.close_admission()
        for registration in registrations:
            await registration.aclose()
