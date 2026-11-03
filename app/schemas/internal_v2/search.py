"""搜索公共生命周期；领域正文、分页和版本继续使用原 owner 的类型。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from app.schemas.internal_v2.session_navigation import SessionCatalogSearchResultsDTO
from app.schemas.internal_v2.session_navigation.thread_search import (
    SessionThreadSearchResultsDTO,
)


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
    limited_reason: Literal["result_limit", "scan_limit", "deadline", "output_limit"] | None = None
    catalog: SessionCatalogSearchResultsDTO | None = None
    threads: SessionThreadSearchResultsDTO | None = None

    @model_validator(mode="after")
    def validate_domain_result(self) -> Self:
        if (self.state == "complete") != (self.limited_reason is None):
            raise ValueError("搜索完成状态与受限原因不一致")
        if self.limited_reason == "output_limit":
            if self.catalog is not None or self.threads is not None:
                raise ValueError("零输出配额不得返回领域结果")
            return self
        if self.category == "catalog" and self.catalog is not None and self.threads is None:
            return self
        if self.category == "thread" and self.threads is not None and self.catalog is None:
            return self
        raise ValueError("搜索类别与领域结果不一致")
