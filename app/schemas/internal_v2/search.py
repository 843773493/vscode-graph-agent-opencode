"""搜索公共生命周期；领域正文、分页和版本继续使用原 owner 的类型。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

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

    @property
    def domain_page(self) -> SessionCatalogSearchResultsDTO | SessionThreadSearchResultsDTO | WorkspaceMessageSearchResultsDTO | None:
        return self.catalog or self.threads or self.messages

    @model_validator(mode="after")
    def validate_domain_result(self) -> Self:
        if (self.state == "complete") != (self.limited_reason is None):
            raise ValueError("搜索完成状态与受限原因不一致")
        if self.limited_reason == "output_limit":
            if self.domain_page is not None:
                raise ValueError("零输出配额不得返回领域结果")
            return self
        expected = {"catalog": self.catalog, "thread": self.threads,
                    "message": self.messages, "tool_result": self.messages}.get(self.category)
        if expected is not None and sum(page is not None for page in (self.catalog, self.threads, self.messages)) == 1:
            return self
        raise ValueError("搜索类别与领域结果不一致")
