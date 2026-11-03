"""跨目标搜索的请求与传输生命周期；领域页保持原 owner 的公开类型。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from app.schemas.gateway.search import (
    GatewaySearchScopeRequest,
    GatewaySearchScopeResolutionDTO,
    GatewaySearchWorkspaceOwnerDTO,
)
from app.schemas.internal_v2.search import WorkspaceSearchResultDTO

MAX_SEARCH_BRANCHES = 128
SEARCH_CONCURRENCY = 3
# 接收领域页和控制状态分别有硬上限；状态不占名称命中数。
# TODO：16.17 补齐包含控制状态的统一输出字节计量，以及正文／文件的独立计数单位。
MAX_SEARCH_PAGE_BYTES = 8 * 1024 * 1024
MAX_SEARCH_ERROR_CHARS = 4096


class FederatedSearchQueryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str = Field(min_length=1, max_length=64)
    operation: str = Field(default="name", min_length=1, max_length=64)
    contract_version: int = Field(default=1, ge=1)
    query: str = Field(min_length=1, max_length=512)
    match_mode: str = Field(default="fuzzy", min_length=1, max_length=64)
    limit: int = Field(default=50, ge=0, le=500)


class FederatedSearchBranchKeyDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: GatewaySearchWorkspaceOwnerDTO
    category: str = Field(min_length=1, max_length=64)
    operation: str = Field(default="name", min_length=1, max_length=64)


class FederatedSearchContinuationDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    branch: FederatedSearchBranchKeyDTO
    cursor: str | None = Field(default=None, max_length=32768)


class FederatedSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: GatewaySearchScopeRequest
    queries: tuple[FederatedSearchQueryDTO, ...] = Field(min_length=1, max_length=8)
    continuations: tuple[FederatedSearchContinuationDTO, ...] = Field(default=(), max_length=MAX_SEARCH_BRANCHES)
    max_results: int = Field(default=200, ge=0, le=1000)
    deadline_at: FiniteFloat | None = None

    @model_validator(mode="after")
    def validate_queries(self) -> Self:
        keys = [(query.category, query.operation) for query in self.queries]
        if len(set(keys)) != len(keys):
            raise ValueError("同一次搜索不得重复声明类别／操作")
        if self.continuations and self.scope.mode != "resume":
            raise ValueError("分支续查或重试必须携带原冻结范围")
        branches = [item.branch for item in self.continuations]
        if len(set(branches)) != len(branches):
            raise ValueError("同一次请求不得重复分发同一分支")
        return self


class FederatedSearchBranchDTO(BaseModel):
    branch: FederatedSearchBranchKeyDTO
    state: Literal["complete", "limited", "failed", "cancelled"]
    provider_id: str | None = None
    code: str | None = None
    message: str | None = None
    retryable: bool = False
    cursor: str | None = None
    allocated_results: int = Field(ge=0)
    returned_results: int = Field(default=0, ge=0)


class FederatedSearchSummaryDTO(BaseModel):
    state: Literal["complete", "incomplete"]
    order: Literal["category-scope-owner-v1"] = "category-scope-owner-v1"
    branches: list[FederatedSearchBranchDTO]
    returned_results: int = Field(ge=0)


class FederatedSearchEventDTO(BaseModel):
    sequence: int = Field(ge=1)
    request_id: str
    type: Literal["scope", "page", "branch", "end"]
    scope: GatewaySearchScopeResolutionDTO | None = None
    branch: FederatedSearchBranchDTO | None = None
    page: WorkspaceSearchResultDTO | None = None
    summary: FederatedSearchSummaryDTO | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        actual = self.scope is not None, self.branch is not None, self.page is not None, self.summary is not None
        expected = {"scope": (True, False, False, False), "page": (False, True, True, False),
                    "branch": (False, True, False, False), "end": (False, False, False, True)}
        if actual != expected[self.type]:
            raise ValueError("搜索事件类型与 payload 不符")
        return self
