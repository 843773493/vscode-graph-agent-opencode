from __future__ import annotations

import json
from collections.abc import Sequence
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from app.agents.tools import web as web_tools
from app.runtime.embeddings import LiteLLMEmbeddingComputer


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


@pytest.mark.asyncio
async def test_litellm_embedding_computer_preserves_response_index_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_aembedding(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            data=[
                {"index": 1, "embedding": [0, 1]},
                {"index": 0, "embedding": [1, 0]},
            ]
        )

    monkeypatch.setattr("litellm.aembedding", fake_aembedding)
    computer = LiteLLMEmbeddingComputer.from_provider(
        {
            "id": "openrouter_embedding",
            "endpoint": "https://openrouter.ai/api/v1",
            "model": "openai/text-embedding-3-small",
            "api_key": "test-key",
            "custom_llm_provider": "openrouter",
        }
    )

    embeddings = await computer.compute(["first", "second"])

    assert embeddings == [[1.0, 0.0], [0.0, 1.0]]
    assert captured["max_retries"] == 3
    assert captured["model"] == "openai/text-embedding-3-small"
