"""搜索公共生命周期；领域正文、分页和版本继续使用原 owner 的类型。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from app.schemas.internal_v2.llm_request_log import (
    WorkspaceLLMRequestLogSearchResultsDTO,
)
from app.schemas.internal_v2.resource_navigation import (
    NavigationResourceURI,
    ResourceNavigationResultDTO,
    ResourceNavigationSearchOptions,
)
from app.schemas.internal_v2.session_context import (
    SessionContextSearchResultDTO,
    SessionContextSearchSource,
)
from app.schemas.internal_v2.session_navigation import SessionCatalogSearchResultsDTO
from app.schemas.internal_v2.session_navigation.thread_search import (
    SessionThreadSearchResultsDTO,
)
from app.schemas.internal_v2.turn_search import WorkspaceMessageSearchResultsDTO


class SearchProviderCapabilityDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_id: str
    category: str
    operations: tuple[str, ...] = Field(min_length=1)
    contract_version: int = Field(ge=1)
    supported_options: tuple[str, ...]
    position_kind: str
    max_results: int = Field(ge=1)
    cache_policy: Literal["owner", "none"]
    match_modes: tuple[str, ...] = Field(min_length=1)


class WorkspaceSearchCapabilitiesDTO(BaseModel):
    workspace_id: str
    providers: list[SearchProviderCapabilityDTO]


class WorkspaceFileSearchTargetDTO(BaseModel):
    """一个原文件根的局部选项；省略 URI 时只访问目标工作区根。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: NavigationResourceURI | None = None
    search_options: ResourceNavigationSearchOptions = Field(default_factory=ResourceNavigationSearchOptions)
    max_bytes: int = Field(default=65536, ge=1, le=33554432)


class WorkspaceContextSearchOptionsDTO(BaseModel):
    """原上下文领域选项，身份由目标 owner 生成。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: tuple[SessionContextSearchSource, ...] = Field(default=("effective_context",), min_length=1)
    case_sensitive: bool = False
    max_chars: int = Field(default=16384, ge=256, le=65536)
    revision: str | None = None


class WorkspaceSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    category: str = Field(min_length=1)
    operation: str = "name"
    contract_version: int = Field(default=1, ge=1)
    query: str = Field(min_length=1)
    match_mode: str = "fuzzy"
    limit: int = Field(default=50, ge=0, le=500)
    cursor: str | None = None
    deadline_at: FiniteFloat | None = None
    file: WorkspaceFileSearchTargetDTO | None = None
    context: WorkspaceContextSearchOptionsDTO | None = None

    @model_validator(mode="after")
    def validate_file_target(self) -> Self:
        if self.file is not None and self.category != "file":
            raise ValueError("只有文件搜索接受文件根和局部选项")
        if self.context is not None and self.category != "context":
            raise ValueError("只有上下文搜索接受来源及字符预算")
        return self


class WorkspaceSearchResultDTO(BaseModel):
    provider_id: str
    workspace_id: str
    category: str
    operation: str
    contract_version: int
    state: Literal["complete", "limited"]
    limited_reason: Literal[
        "result_limit", "scan_limit", "thread_limit", "byte_limit", "deadline", "item_size_limit", "output_limit",
    ] | None = None
    catalog: SessionCatalogSearchResultsDTO | None = None
    threads: SessionThreadSearchResultsDTO | None = None
    messages: WorkspaceMessageSearchResultsDTO | None = None
    llm_requests: WorkspaceLLMRequestLogSearchResultsDTO | None = None
    files: ResourceNavigationResultDTO | None = None
    context: SessionContextSearchResultDTO | None = None

    @property
    def domain_page(self) -> SessionCatalogSearchResultsDTO | SessionThreadSearchResultsDTO | WorkspaceMessageSearchResultsDTO | WorkspaceLLMRequestLogSearchResultsDTO | ResourceNavigationResultDTO | SessionContextSearchResultDTO | None:
        return self.catalog or self.threads or self.messages or self.llm_requests or self.files or self.context

    @property
    def result_count(self) -> int:
        page = self.domain_page
        if isinstance(page, SessionContextSearchResultDTO):
            return len(page.matches)
        if isinstance(page, ResourceNavigationResultDTO):
            return len(page.matches) if self.operation == "content" else len(page.entries)
        return len(page.items) if page is not None else 0

    @property
    def cursor(self) -> str | None:
        page = self.domain_page
        if isinstance(page, (ResourceNavigationResultDTO, SessionContextSearchResultDTO)):
            return page.next_cursor
        return page.cursor if page is not None else None

    @model_validator(mode="after")
    def validate_domain_result(self) -> Self:
        if (self.state == "complete") != (self.limited_reason is None):
            raise ValueError("搜索完成状态与受限原因不一致")
        if self.limited_reason == "output_limit":
            if self.domain_page is not None:
                raise ValueError("零输出配额不得返回领域结果")
            return self
        expected = {"catalog": self.catalog, "thread": self.threads,
                    "message": self.messages, "tool_result": self.messages, "llm_request": self.llm_requests,
                    "file": self.files, "context": self.context}.get(self.category)
        if expected is not None and sum(page is not None for page in (self.catalog, self.threads, self.messages, self.llm_requests, self.files, self.context)) == 1:
            return self
        raise ValueError("搜索类别与领域结果不一致")
