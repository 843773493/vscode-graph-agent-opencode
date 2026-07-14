from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlparse

import httpx
from langchain_community.tools import DuckDuckGoSearchResults
from langchain_community.utilities import DuckDuckGoSearchAPIWrapper
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from app.abstractions.embeddings import EmbeddingComputerProtocol
from app.agents.custom_tools import CustomToolFactoryContext
from app.runtime.embeddings import LiteLLMEmbeddingComputer


_MAX_DOWNLOAD_BYTES = 2_000_000
_CHUNK_CHARS = 1_400
_EMBEDDING_BATCH_SIZE = 100
_TEXT_CONTENT_TYPES = (
    "application/json",
    "application/javascript",
    "application/xhtml+xml",
    "application/xml",
)


class WebSearchInput(BaseModel):
    """DuckDuckGo Web 搜索参数。"""

    query: str = Field(description="搜索查询。")
    max_results: int = Field(default=5, ge=1, le=10, description="最多返回的结果数。")
    search_type: Literal["text", "news"] = Field(
        default="text",
        description="普通网页搜索或新闻搜索。",
    )
    region: str = Field(default="wt-wt", description="DuckDuckGo 区域代码。")
    safesearch: Literal["on", "moderate", "off"] = Field(
        default="moderate",
        description="安全搜索级别。",
    )
    time_range: Literal["d", "w", "m", "y"] | None = Field(
        default=None,
        description="可选时间范围：日、周、月、年。",
    )


class FetchWebPageInput(BaseModel):
    """批量抓取网页正文参数。"""

    urls: list[str] = Field(
        min_length=1,
        max_length=5,
        description="要抓取的 HTTP/HTTPS URL，最多 5 个。",
    )
    query: str | None = Field(
        default=None,
        description="可选相关性查询；提供后优先返回与查询相关的页面片段。",
    )
    max_chars_per_page: int = Field(
        default=6_000,
        ge=1_000,
        le=20_000,
        description="每个页面最多返回的正文字符数。",
    )


class _EmbeddingOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1)


class _FetchWebpageToolOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    embedding: _EmbeddingOptions | None = None


class _SemanticChunkRanker:
    def __init__(
        self,
        *,
        query: str,
        computer: EmbeddingComputerProtocol,
    ) -> None:
        self.query = query
        self.provider_id = computer.provider_id
        self.model = computer.model
        self._computer = computer
        self._query_embedding_task: asyncio.Task[list[float]] | None = None

    async def score(self, chunks: Sequence[str]) -> list[float]:
        query_embedding, chunk_embeddings = await asyncio.gather(
            self._get_query_embedding(),
            self._compute_chunk_embeddings(chunks),
        )
        return [
            _cosine_similarity(query_embedding, chunk_embedding)
            for chunk_embedding in chunk_embeddings
        ]

    async def _get_query_embedding(self) -> list[float]:
        if self._query_embedding_task is None:
            self._query_embedding_task = asyncio.create_task(
                self._compute_query_embedding()
            )
        return await self._query_embedding_task

    async def _compute_query_embedding(self) -> list[float]:
        embeddings = await self._computer.compute([self.query])
        if len(embeddings) != 1:
            raise RuntimeError(
                f"查询 Embedding 返回数量错误: expected=1, actual={len(embeddings)}"
            )
        return embeddings[0]

    async def _compute_chunk_embeddings(
        self,
        chunks: Sequence[str],
    ) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for start in range(0, len(chunks), _EMBEDDING_BATCH_SIZE):
            batch = chunks[start:start + _EMBEDDING_BATCH_SIZE]
            embeddings.extend(await self._computer.compute(batch))
        if len(embeddings) != len(chunks):
            raise RuntimeError(
                "正文 Embedding 返回数量错误: "
                f"expected={len(chunks)}, actual={len(embeddings)}"
            )
        return embeddings


class _VisibleTextParser(HTMLParser):
    _SKIPPED_TAGS = {"script", "style", "noscript", "svg", "template"}
    _BLOCK_TAGS = {
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "p",
        "pre",
        "section",
        "table",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._text_parts: list[str] = []
        self._title_parts: list[str] = []

    @property
    def title(self) -> str | None:
        value = _normalize_inline_text(" ".join(self._title_parts))
        return value or None

    @property
    def text(self) -> str:
        return _normalize_document_text("".join(self._text_parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized_tag = tag.lower()
        if normalized_tag in self._SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if normalized_tag == "title":
            self._in_title = True
        if normalized_tag in self._BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in self._SKIPPED_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if normalized_tag == "title":
            self._in_title = False
        if normalized_tag in self._BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        else:
            self._text_parts.append(data)


def _normalize_inline_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_document_text(value: str) -> str:
    lines = [_normalize_inline_text(line) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _validated_web_url(raw_url: str) -> str:
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"只支持包含主机名的 HTTP/HTTPS URL: {raw_url!r}")
    if parsed.username or parsed.password:
        raise ValueError(f"URL 不允许包含用户名或密码: {raw_url!r}")
    return url


def _content_type_is_text(content_type: str) -> bool:
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type.startswith("text/") or media_type in _TEXT_CONTENT_TYPES


def _split_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    for paragraph in text.splitlines():
        if current and current_chars + len(paragraph) + 1 > _CHUNK_CHARS:
            chunks.append("\n".join(current))
            current = []
            current_chars = 0
        if len(paragraph) > _CHUNK_CHARS:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_chars = 0
            chunks.extend(
                paragraph[index:index + _CHUNK_CHARS]
                for index in range(0, len(paragraph), _CHUNK_CHARS)
            )
            continue
        current.append(paragraph)
        current_chars += len(paragraph) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError(
            f"Embedding 向量维度不一致: left={len(left)}, right={len(right)}"
        )
    dot_product = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("Embedding API 返回了零向量，无法计算余弦相似度")
    return dot_product / (left_norm * right_norm)


async def _select_page_text(
    text: str,
    max_chars: int,
    ranker: _SemanticChunkRanker | None,
) -> tuple[str, bool]:
    chunks = _split_chunks(text)
    if ranker is not None and chunks:
        scores = await ranker.score(chunks)
        ranked = sorted(
            enumerate(chunks),
            key=lambda item: (scores[item[0]], -item[0]),
            reverse=True,
        )
        selected_indexes: list[int] = []
        selected_chars = 0
        for index, chunk in ranked:
            if selected_indexes and selected_chars + len(chunk) + 2 > max_chars:
                continue
            selected_indexes.append(index)
            selected_chars += len(chunk) + 2
            if selected_chars >= max_chars:
                break
        chunks = [chunks[index] for index in sorted(selected_indexes)]

    selected = "\n\n".join(chunks)
    if len(selected) <= max_chars:
        return selected, len(selected) < len(text)
    return selected[:max_chars], True


async def _fetch_page(
    client: httpx.AsyncClient,
    url: str,
    max_chars: int,
    ranker: _SemanticChunkRanker | None,
) -> dict[str, object]:
    validated_url = _validated_web_url(url)
    try:
        async with client.stream("GET", validated_url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if not _content_type_is_text(content_type):
                raise ValueError(
                    f"fetch_webpage 只支持文本响应: url={validated_url}, "
                    f"content_type={content_type or 'unknown'}"
                )
            body = bytearray()
            download_truncated = False
            async for chunk in response.aiter_bytes():
                remaining = _MAX_DOWNLOAD_BYTES - len(body)
                if remaining <= 0:
                    download_truncated = True
                    break
                body.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    download_truncated = True
                    break
            encoding = response.charset_encoding or "utf-8"
            raw_text = bytes(body).decode(encoding, errors="replace")
            title: str | None = None
            if "html" in content_type.lower():
                parser = _VisibleTextParser()
                parser.feed(raw_text)
                title = parser.title
                page_text = parser.text
            else:
                page_text = _normalize_document_text(raw_text)
            selected_text, output_truncated = await _select_page_text(
                page_text,
                max_chars,
                ranker,
            )
            return {
                "url": validated_url,
                "final_url": str(response.url),
                "status_code": response.status_code,
                "content_type": content_type.partition(";")[0].strip().lower(),
                "title": title,
                "content": selected_text,
                "content_is_untrusted": True,
                "content_selection": (
                    "semantic_embedding" if ranker is not None else "document_order"
                ),
                "page_chars": len(page_text),
                "returned_chars": len(selected_text),
                "download_truncated": download_truncated,
                "content_truncated": output_truncated or download_truncated,
            }
    except httpx.HTTPError as error:
        raise RuntimeError(f"抓取网页失败: url={validated_url}, error={error}") from error


def create_web_search_tool(context: CustomToolFactoryContext) -> BaseTool:
    """创建 DuckDuckGo Web 搜索扩展工具。"""

    del context

    async def web_search(
        query: str,
        max_results: int = 5,
        search_type: Literal["text", "news"] = "text",
        region: str = "wt-wt",
        safesearch: Literal["on", "moderate", "off"] = "moderate",
        time_range: Literal["d", "w", "m", "y"] | None = None,
    ) -> str:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query 不能为空")
        wrapper = DuckDuckGoSearchAPIWrapper(
            region=region,
            safesearch=safesearch,
            time=time_range,
            source=search_type,
        )
        search = DuckDuckGoSearchResults(
            output_format="list",
            num_results=max_results,
            backend=search_type,
            api_wrapper=wrapper,
        )
        try:
            loop = asyncio.get_running_loop()
            raw_results = await loop.run_in_executor(
                None,
                search.invoke,
                normalized_query,
            )
        except Exception as error:
            raise RuntimeError(f"DuckDuckGo 搜索失败: query={normalized_query!r}, error={error}") from error
        if not isinstance(raw_results, list):
            raise TypeError(f"DuckDuckGo 返回了非列表结果: {type(raw_results).__name__}")
        results = []
        for result in raw_results:
            if not isinstance(result, dict):
                raise TypeError(f"DuckDuckGo 结果项不是对象: {type(result).__name__}")
            item = dict(result)
            if "link" in item:
                item["url"] = item.pop("link")
            results.append(item)
        return json.dumps(
            {
                "query": normalized_query,
                "provider": "duckduckgo",
                "search_type": search_type,
                "returned_result_count": len(results),
                "results": results,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    return StructuredTool.from_function(
        coroutine=web_search,
        name="web_search",
        description=(
            "使用 DuckDuckGo 搜索公开网页或近期新闻，返回标题、URL、摘要和来源元数据。"
            "需要完整正文时，再把结果 URL 交给 fetch_webpage。"
        ),
        args_schema=WebSearchInput,
    )


def create_fetch_webpage_tool(context: CustomToolFactoryContext) -> BaseTool:
    """创建批量网页正文抓取扩展工具。"""

    raw_options = context.tool_options
    if not isinstance(raw_options, Mapping):
        raise TypeError("fetch_webpage 工具 options 必须是对象")
    options = _FetchWebpageToolOptions.model_validate(dict(raw_options))

    embedding_computer: EmbeddingComputerProtocol | None = None
    if options.embedding is not None:
        provider = context.config_service.get_llm_provider(
            options.embedding.provider_id
        )
        embedding_computer = LiteLLMEmbeddingComputer.from_provider(
            provider,
            model=options.embedding.model,
        )

    async def fetch_webpage(
        urls: list[str],
        query: str | None = None,
        max_chars_per_page: int = 6_000,
    ) -> str:
        normalized_query = _normalize_inline_text(query or "") or None
        ranker: _SemanticChunkRanker | None = None
        if normalized_query is not None:
            if embedding_computer is None:
                raise RuntimeError(
                    "fetch_webpage 收到了 query，但未配置语义排序。请在该工具的 "
                    "options.embedding 中配置 provider_id 和可选 model。"
                )
            ranker = _SemanticChunkRanker(
                query=normalized_query,
                computer=embedding_computer,
            )

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "BoxTeam-WebFetch/1.0"},
        ) as client:
            pages = await asyncio.gather(
                *(
                    _fetch_page(client, url, max_chars_per_page, ranker)
                    for url in urls
                )
            )
        selection: dict[str, object] = {"strategy": "document_order"}
        if ranker is not None:
            selection = {
                "strategy": "semantic_embedding",
                "provider_id": ranker.provider_id,
                "model": ranker.model,
            }
        return json.dumps(
            {
                "query": normalized_query,
                "content_is_untrusted": True,
                "content_selection": selection,
                "requested_url_count": len(urls),
                "returned_page_count": len(pages),
                "pages": pages,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    return StructuredTool.from_function(
        coroutine=fetch_webpage,
        name="fetch_webpage",
        description=(
            "抓取最多 5 个 HTTP/HTTPS 网页的可见文本。可传 query，"
            "通过远程 Embedding 语义排序优先返回与任务相关的正文片段，"
            "并提供来源、排序方式和截断元数据。"
        ),
        args_schema=FetchWebPageInput,
    )
