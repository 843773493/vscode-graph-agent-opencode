"""跨目标搜索的请求与传输生命周期；领域页保持原 owner 的公开类型。"""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from app.schemas.gateway.search import (
    GatewaySearchScopeRequest,
    GatewaySearchScopeResolutionDTO,
    GatewaySearchWorkspaceOwnerDTO,
)
from app.schemas.internal_v2.resource_navigation import NavigationResourceURI
from app.schemas.internal_v2.search import (
    WorkspaceFileSearchTargetDTO,
    WorkspaceSearchResultDTO,
)

MAX_SEARCH_BRANCHES = 128
SEARCH_CONCURRENCY = 3
# 接收整页与编码输出各有总预算；输出包括 scope、游标、状态及 SSE 帧，状态不占命中数。
MAX_SEARCH_PAGE_BYTES = 8 * 1024 * 1024
MAX_SEARCH_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_SEARCH_ERROR_CHARS = 4096


class FederatedFileSearchTargetDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owner: GatewaySearchWorkspaceOwnerDTO
    target: WorkspaceFileSearchTargetDTO


class FederatedSearchQueryDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str = Field(min_length=1, max_length=64)
    operation: str = Field(default="name", min_length=1, max_length=64)
    contract_version: int = Field(default=1, ge=1)
    query: str = Field(min_length=1, max_length=512)
    match_mode: str = Field(default="fuzzy", min_length=1, max_length=64)
    limit: int = Field(default=50, ge=0, le=500)
    file_targets: tuple[FederatedFileSearchTargetDTO, ...] = Field(default=(), max_length=MAX_SEARCH_BRANCHES)
    allow_catalog_mirror: bool = False

    @model_validator(mode="after")
    def validate_file_targets(self) -> Self:
        if self.allow_catalog_mirror and (self.category, self.operation, self.contract_version, self.match_mode) != ("catalog", "name", 1, "fuzzy"):
            raise ValueError("离线目录镜像只支持 catalog/name v1 fuzzy 合同")
        if self.file_targets and self.category != "file":
            raise ValueError("只有文件搜索接受目录根集合")
        keys = [(item.owner, item.target.uri) for item in self.file_targets]
        if len(set(keys)) != len(keys):
            raise ValueError("文件根不得重复声明；不同局部选项不能隐式覆盖")
        return self


class FederatedSearchBranchKeyDTO(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")

    owner: GatewaySearchWorkspaceOwnerDTO
    category: str = Field(min_length=1, max_length=64)
    operation: str = Field(default="name", min_length=1, max_length=64)
    resource_uri: NavigationResourceURI | None = None

    @model_validator(mode="after")
    def validate_resource_branch(self) -> Self:
        if self.resource_uri is not None and self.category != "file":
            raise ValueError("只有文件分支接受目录根身份")
        return self


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


class FederatedSearchSourceDTO(BaseModel):
    kind: Literal["owner", "gateway_catalog_mirror"] = "owner"
    updated_at: datetime | None = None
    stale: bool = False
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if self.kind == "gateway_catalog_mirror":
            if not self.stale or self.updated_at is None or self.updated_at.tzinfo is None or not self.unavailable_reason:
                raise ValueError("离线镜像必须披露更新时间、stale 和原目标不可用原因")
        elif self.stale or self.updated_at is not None or self.unavailable_reason is not None:
            raise ValueError("在线 owner 不接受镜像状态")
        return self


class FederatedSearchEventDTO(BaseModel):
    sequence: int = Field(ge=1)
    request_id: str
    type: Literal["scope", "page", "branch", "end"]
    scope: GatewaySearchScopeResolutionDTO | None = None
    branch: FederatedSearchBranchDTO | None = None
    page: WorkspaceSearchResultDTO | None = None
    summary: FederatedSearchSummaryDTO | None = None
    source: FederatedSearchSourceDTO | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        actual = self.scope is not None, self.branch is not None, self.page is not None, self.summary is not None
        expected = {"scope": (True, False, False, False), "page": (False, True, True, False),
                    "branch": (False, True, False, False), "end": (False, False, False, True)}
        if actual != expected[self.type]:
            raise ValueError("搜索事件类型与 payload 不符")
        if self.source is not None and self.type != "page":
            raise ValueError("只有搜索页携带来源信息")
        return self
