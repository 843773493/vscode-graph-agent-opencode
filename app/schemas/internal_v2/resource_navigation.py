"""与 workspace/v2/public.proto 一致的只读资源导航 HTTP 边界。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

from app.core.resource_uri import ResourceUriGrammarError, parse_resource_uri
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceNavigationOperation,
    ResourceShape,
)


def _require_navigation_uri(value: str) -> str:
    try:
        parse_resource_uri(value)
    except ResourceUriGrammarError as error:
        # 输出合同故障只报告字段与原因；原生输入和文件正文不走此校验。
        raise ValueError(f"导航资源引用必须是资源 URI: code={error.reason_code}") from error
    return value


NavigationResourceURI = Annotated[
    str,
    AfterValidator(_require_navigation_uri),
    # 归属投影使用model_copy；构造后引用仍须在HTTP/联邦/模型共用的发布点核验。
    PlainSerializer(_require_navigation_uri, return_type=str),
]


class ResourceNavigationSearchOptions(BaseModel):
    """原文件搜索的显式选项；省略时保留原导航和工具行为。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_mode: Literal["sensitive", "insensitive", "smart"] = "sensitive"
    is_regex: bool = False
    whole_word: bool = False
    word_separators: str | None = None
    is_multiline: bool = False
    all_matches: bool = False
    files_only: bool = False
    include_globs: tuple[str, ...] = Field(default=(), max_length=64)
    exclude_globs: tuple[str, ...] = Field(default=(), max_length=64)
    ignore_mode: Literal["default", "none", "respect"] = "default"
    context_lines: int = Field(default=0, ge=0, le=100)
    excluded_resource_uris: tuple[NavigationResourceURI, ...] = Field(default=(), max_length=10000)
    excluded_subtree_uris: tuple[NavigationResourceURI, ...] = Field(default=(), max_length=256)

    @field_validator("excluded_resource_uris", "excluded_subtree_uris")
    @classmethod
    def normalize_excluded_resources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(values)))

    @model_validator(mode="after")
    def require_nonempty_patterns(self) -> ResourceNavigationSearchOptions:
        if any(not pattern or len(pattern) > 4096 for pattern in (*self.include_globs, *self.exclude_globs)):
            raise ValueError("搜索路径模式须为非空字符串且不超过4096字符")
        return self


class ResourceNavigationBudget(BaseModel):
    """固定根只读发现和文件导航共用的执行与输出预算。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(default=100, ge=1, le=1000)
    timeout_ms: int = Field(default=10000, ge=1, le=30000)
    scan_limit: int = Field(default=10000, ge=1, le=100000)
    max_response_bytes: int = Field(default=1048576, ge=1024, le=50331648)


class ResourceNavigationRequest(ResourceNavigationBudget):
    uri: str = Field(min_length=1)
    operation: ResourceNavigationOperation
    query: str | None = None
    max_bytes: int = Field(default=65536, ge=1, le=33554432)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    read_format: Literal["text", "auto"] = "text"
    line_offset: int | None = Field(default=None, ge=1)
    max_lines: int | None = Field(default=None, ge=1)
    search_mode: Literal["path", "fuzzy", "glob", "content"] = "path"
    file_glob: str | None = Field(default=None, min_length=1)
    max_snippet_bytes: int = Field(default=2048, ge=16, le=8192)
    search_options: ResourceNavigationSearchOptions | None = None

    @model_validator(mode="after")
    def require_search_query(self) -> ResourceNavigationRequest:
        if self.operation == "search":
            if not self.query:
                raise ValueError("search 必须提供非空 query")
        elif self.query is not None:
            raise ValueError("只有 search 接受 query")
        if self.cursor is not None and self.operation not in {"list", "search"}:
            raise ValueError("只有 list/search 接受 cursor")
        if self.operation != "read" and (self.read_format != "text" or self.line_offset is not None or self.max_lines is not None):
            raise ValueError("只有 read 接受格式和行范围")
        if self.operation != "search" and (self.search_mode != "path" or self.file_glob is not None):
            raise ValueError("只有 search 接受搜索模式和文件过滤")
        if self.file_glob is not None and self.search_mode != "content":
            raise ValueError("只有内容搜索接受 file_glob")
        if self.search_options is not None:
            if self.operation != "search":
                raise ValueError("只有 search 接受 search_options")
            if self.search_mode != "content" and any((
                self.search_options.case_mode != "sensitive", self.search_options.is_regex,
                self.search_options.whole_word, self.search_options.word_separators is not None,
                self.search_options.is_multiline, self.search_options.all_matches,
                self.search_options.context_lines,
                self.search_options.excluded_resource_uris,
            )):
                raise ValueError("只有内容搜索接受文本匹配选项")
        return self


class ResourceNavigationStatDTO(BaseModel):
    """目标文件系统的轻量观察值；不表示内容版本或快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    size: int = Field(ge=0)
    modified_at_ms: float
    changed_at_ms: float


class ResourceNavigationNodeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True, hide_input_in_errors=True)

    uri: NavigationResourceURI
    resource_shape: ResourceShape
    operations: tuple[ResourceNavigationOperation, ...]
    child_path: Literal["file_segments"] | None = None
    stat: ResourceNavigationStatDTO | None = None


class ResourceNavigationMatchDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    uri: NavigationResourceURI
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    text: str
    snippet_start_column: int = Field(ge=1)
    truncated: bool = False
    end_line: int | None = Field(default=None, ge=1)
    end_column: int | None = Field(default=None, ge=1)
    utf16_column: int | None = Field(default=None, ge=1)
    utf16_end_column: int | None = Field(default=None, ge=1)
    snippet_start_utf16_column: int | None = Field(default=None, ge=1)


class ResourceNavigationContextDTO(BaseModel):
    """同一读取中的上下文行，不充当额外匹配或独立内容版本。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    uri: NavigationResourceURI
    line: int = Field(ge=1)
    text: str
    truncated: bool = False


class ResourceNavigationResultDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    node: ResourceNavigationNodeDTO
    entries: tuple[ResourceNavigationNodeDTO, ...] = ()
    content: str | None = None
    truncated: bool = False
    next_cursor: str | None = None
    truncation_reason: Literal["entry_limit", "response_byte_limit", "file_byte_limit", "line_limit"] | None = None
    view: Literal["live"] = "live"
    content_encoding: Literal["utf-8", "base64"] | None = None
    media_type: Literal["text", "image", "audio", "video", "file"] | None = None
    mime_type: str | None = None
    line_offset: int | None = None
    matches: tuple[ResourceNavigationMatchDTO, ...] = ()
    content_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    contexts: tuple[ResourceNavigationContextDTO, ...] = ()
