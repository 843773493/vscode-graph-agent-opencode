from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from unittest.mock import MagicMock

import httpx
import pytest

from app.agents.tools import web as web_tools


@pytest.fixture(autouse=True)
def _hermetic_proxy_env(monkeypatch: pytest.MonkeyPatch):
    """R19 测试内 hermetic 修复：清掉本机代理环境变量。

    本文件用例全部 monkeypatch 掉真实抓取（``_fetch_page``/搜索工具），
    自身不经任何网络；而环境的 NO_PROXY 含 IPv6 字面量（``::1``/
    ``[::1]``）会让 httpx 客户端构造时（``create_fetch_webpage_tool`` 内
    建 AsyncClient）的代理解析直接抛 ``InvalidURL: Invalid port: ':1]'``
    ——属外部环境噪声混入测试。与 R18 对 gateway 会话上下文客户端的
    同类修复一致（任务书许可的 monkeypatch 清 ``*_proxy`` 做法）；清掉
    后客户端不取代理，与用例意图一致。
    """
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


class _FakeEmbeddingComputer:
    provider_id = "embedding_test"
    model = "semantic-test-model"

    async def compute(self, inputs: Sequence[str]) -> list[list[float]]:
        return [
            [1.0, 0.0] if "uv sync" in value or value == "dependencies install" else [0.0, 1.0]
            for value in inputs
        ]


@pytest.mark.asyncio
async def test_web_search_returns_structured_duckduckgo_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeDuckDuckGoSearchResults:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["output_format"] == "list"
            assert kwargs["num_results"] == 2

        def invoke(self, query: str) -> list[dict[str, str]]:
            assert query == "Python official website"
            return [
                {
                    "title": "Python",
                    "link": "https://www.python.org/",
                    "snippet": "The official home of Python.",
                }
            ]

    monkeypatch.setattr(
        web_tools,
        "DuckDuckGoSearchResults",
        FakeDuckDuckGoSearchResults,
    )
    tool = web_tools.create_web_search_tool(MagicMock())

    result = json.loads(
        await tool.ainvoke(
            {
                "query": "Python official website",
                "max_results": 2,
                "time_range": None,
            }
        )
    )

    assert result == {
        "query": "Python official website",
        "provider": "duckduckgo",
        "search_type": "text",
        "returned_result_count": 1,
        "results": [
            {
                "title": "Python",
                "snippet": "The official home of Python.",
                "url": "https://www.python.org/",
            }
        ],
    }


@pytest.mark.asyncio
async def test_fetch_page_extracts_visible_html_and_prioritizes_query() -> None:
    irrelevant = "unrelated navigation " * 120
    html = f"""
    <html>
      <head><title>Example documentation</title><style>hidden style</style></head>
      <body>
        <nav>{irrelevant}</nav>
        <script>hidden script</script>
        <main><h1>Install Guide</h1><p>Use uv sync to install dependencies.</p></main>
      </body>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
            text=html,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        ranker = web_tools._SemanticChunkRanker(
            query="dependencies install",
            computer=_FakeEmbeddingComputer(),
        )
        result = await web_tools._fetch_page(
            client,
            "https://example.com/docs",
            1_000,
            ranker,
        )

    assert result["title"] == "Example documentation"
    assert result["final_url"] == "https://example.com/docs"
    assert "uv sync to install dependencies" in str(result["content"])
    assert "hidden script" not in str(result["content"])
    assert result["content_is_untrusted"] is True
    assert result["content_selection"] == "semantic_embedding"
    assert result["returned_chars"] <= 1_000
    assert result["content_truncated"] is True


@pytest.mark.asyncio
async def test_fetch_webpage_returns_all_page_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_page(
        client: httpx.AsyncClient,
        url: str,
        max_chars: int,
        ranker: web_tools._SemanticChunkRanker | None,
    ) -> dict[str, object]:
        assert isinstance(client, httpx.AsyncClient)
        assert max_chars == 1_500
        assert ranker is not None
        assert ranker.query == "needle"
        return {
            "url": url,
            "final_url": url,
            "status_code": 200,
            "content": f"needle in {url}",
        }

    monkeypatch.setattr(web_tools, "_fetch_page", fake_fetch_page)
    context = MagicMock()
    context.tool_options = {
        "embedding": {
            "provider_id": "embedding_test",
            "model": "semantic-test-model",
        }
    }
    context.config_service.get_llm_provider.return_value = {
        "id": "embedding_test",
        "endpoint": "https://example.com/v1",
        "model": "unused",
        "api_key": "test-key",
        "custom_llm_provider": "openai",
    }
    monkeypatch.setattr(
        web_tools.LiteLLMEmbeddingComputer,
        "from_provider",
        lambda provider, model=None: _FakeEmbeddingComputer(),
    )
    tool = web_tools.create_fetch_webpage_tool(context)
    urls = ["https://example.com/a", "https://example.com/b"]

    result = json.loads(
        await tool.ainvoke(
            {
                "urls": urls,
                "query": "needle",
                "max_chars_per_page": 1_500,
            }
        )
    )

    assert result["requested_url_count"] == 2
    assert result["returned_page_count"] == 2
    assert result["content_is_untrusted"] is True
    assert result["content_selection"] == {
        "strategy": "semantic_embedding",
        "provider_id": "embedding_test",
        "model": "semantic-test-model",
    }
    assert [page["url"] for page in result["pages"]] == urls


@pytest.mark.asyncio
async def test_fetch_page_rejects_binary_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "image/png"},
            content=b"not-an-image",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="只支持文本响应"):
            await web_tools._fetch_page(
                client,
                "https://example.com/image.png",
                1_000,
                None,
            )


def test_fetch_page_rejects_url_credentials() -> None:
    with pytest.raises(ValueError, match="不允许包含用户名或密码"):
        web_tools._validated_web_url("https://user:secret@example.com/")


@pytest.mark.asyncio
async def test_fetch_webpage_query_requires_embedding_configuration() -> None:
    context = MagicMock()
    context.tool_options = {}
    tool = web_tools.create_fetch_webpage_tool(context)

    with pytest.raises(RuntimeError, match="未配置语义排序"):
        await tool.ainvoke(
            {
                "urls": ["https://example.com"],
                "query": "semantic query",
            }
        )


@pytest.mark.parametrize("tool_name", ["web_search", "fetch_webpage"])
@pytest.mark.parametrize("location", ["/private/web-library-cache", r"C:\private\web-library-cache", r"\\target\private\web-library-cache"])
async def test_web_dependency_failure_preserves_cause_without_publishing_native_locations(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tool_name: str, location: str,
) -> None:
    native = PermissionError(13, "外部库无法读取本机文件: " + location, location)
    url = "https://web-failure.invalid/document"
    dependency_error = httpx.ConnectError(str(native), request=httpx.Request("GET", url)) if tool_name == "fetch_webpage" else native
    observed: list[str] = []

    def search(_self: object, query: str) -> list[dict[str, str]]:
        observed.append(query)
        raise dependency_error

    handle = httpx.AsyncHTTPTransport.handle_async_request

    async def fetch(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        if str(request.url) == url:
            observed.append(str(request.url))
            raise dependency_error from native
        return await handle(transport, request)

    monkeypatch.setattr(web_tools.DuckDuckGoSearchResults, "invoke", search)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fetch)
    context = MagicMock()
    context.tool_options = {}
    tool = web_tools.create_web_search_tool(context) if tool_name == "web_search" else web_tools.create_fetch_webpage_tool(context)
    arguments = {"query": "  Python documentation  "} if tool_name == "web_search" else {"urls": [url]}
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as caught:
        await tool.ainvoke(arguments)
    assert caught.value.__cause__ is dependency_error
    assert location not in str(caught.value) and "web-library-cache" not in str(caught.value)
    assert f"error={type(dependency_error).__name__}" in str(caught.value)
    assert ("Python documentation" if tool_name == "web_search" else url) in str(caught.value)
    assert observed == ["Python documentation" if tool_name == "web_search" else url]
    assert any(record.exc_info and record.exc_info[1] is dependency_error for record in caplog.records)
    assert location in caplog.text
