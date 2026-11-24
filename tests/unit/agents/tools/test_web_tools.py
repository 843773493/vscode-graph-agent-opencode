from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.agents.tools import web as web_tools
from app.runtime.network_proxy import ProxyRoutes


@pytest.fixture
def tool_context() -> MagicMock:
    context = MagicMock()
    context.config_service.resolve_workspace_proxy_routes = AsyncMock(return_value=ProxyRoutes(()))
    return context


@pytest.mark.parametrize("tool_name", ["web_search", "fetch_webpage"])
def test_skill_parameter_contract_matches_live_tool_schema(tool_name: str) -> None:
    skill = (Path.cwd() / "resources/skills/web-search-fetch/SKILL.md").read_text()
    documented = {item["tool_name"]: item["arguments_schema"] for item in (
        json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", skill, re.DOTALL)
    )}[tool_name]
    model = web_tools.WebSearchInput if tool_name == "web_search" else web_tools.FetchWebPageInput
    live = model.model_json_schema()
    actual_validator = Draft202012Validator(live)
    documented_validator = Draft202012Validator(documented)
    if tool_name == "web_search":
        examples = [{}, {"query": ""}, {"query": "Python"}, {"query": "Python", "time_range": None}]
        for key, values in {"max_results": [0, 1, 10, 11], "search_type": ["text", "news", "invalid"],
                            "safesearch": ["on", "off", "moderate", "invalid"], "time_range": ["d", "y", "invalid"]}.items():
            examples.extend({"query": "Python", key: value} for value in values)
    else:
        examples = [{}, {"urls": []}, {"urls": [" HTTP://example.com "]}, {"urls": ["x"] * 5}, {"urls": ["x"] * 6}]
        examples.extend({"urls": ["https://example.com"], "max_chars_per_page": value} for value in (999, 1000, 20000, 20001))
        examples.extend({"urls": ["https://example.com"], "query": value} for value in (None, "C++", 1))
    for payload in examples:
        assert documented_validator.is_valid(payload) == actual_validator.is_valid(payload), payload
    assert documented.get("additionalProperties") == live.get("additionalProperties")
    assert documented["required"] == live["required"]
    assert set(documented["properties"]) == set(live["properties"])
    for key, schema in documented["properties"].items():
        assert schema.get("default") == live["properties"][key].get("default")


@pytest.fixture(autouse=True)
def _hermetic_proxy_env(monkeypatch: pytest.MonkeyPatch):
    """显式替换外部依赖的单元测试不采用开发机代理。"""
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


@pytest.mark.asyncio
async def test_web_search_returns_structured_duckduckgo_results(
    monkeypatch: pytest.MonkeyPatch, tool_context: MagicMock,
) -> None:
    def search(query: str, **kwargs: object) -> list[dict[str, str]]:
        assert query == "Python official website"
        assert kwargs["max_results"] == 2
        assert kwargs["routes"] == ProxyRoutes(())
        return [{"title": "Python", "url": "https://www.python.org/", "snippet": "The official home of Python."}]

    monkeypatch.setattr(
        web_tools,
        "search_web",
        search,
    )
    tool = web_tools.create_web_search_tool(tool_context)

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
        "content_is_untrusted": True,
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
async def test_fetch_page_extracts_main_content_without_navigation() -> None:
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
        result = await web_tools._fetch_page(
            client,
            "https://example.com/docs",
        )

    assert result["title"] == "Example documentation"
    assert result["final_url"] == "https://example.com/docs"
    assert "uv sync to install dependencies" in str(result["content"])
    assert "hidden script" not in str(result["content"])
    assert "unrelated navigation" not in str(result["content"])
    assert result["content_is_untrusted"] is True
    assert result["page_chars"] == len(str(result["content"]))
    assert result["download_truncated"] is False


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
            )


def test_fetch_page_rejects_url_credentials() -> None:
    with pytest.raises(ValueError, match="不允许包含用户名或密码"):
        web_tools._validated_web_url("https://user:secret@example.com/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding", "declaration", "header"),
    [
        ("utf-8", "", "text/html"),
        ("gbk", '<meta charset="gbk">', "text/html"),
        ("gbk", '<meta http-equiv="Content-Type" content="text/html; charset=gbk">', "text/html"),
        ("utf-8", '<meta charset="gbk">', "text/html; charset=utf-8"),
    ],
)
async def test_fetch_page_respects_http_and_html_encodings(
    encoding: str, declaration: str, header: str,
) -> None:
    source = (
        f"<html><head>{declaration}<title>安装指南</title></head>"
        "<body><article><p>中文文档安装步骤说明。</p></article></body></html>"
    ).encode(encoding)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": header}, content=source),
    )) as client:
        page = await web_tools._fetch_page(client, "https://example.com/docs")
    assert page["title"] == "安装指南"
    assert "中文文档安装步骤说明。" in str(page["content"])


@pytest.mark.asyncio
async def test_fetch_page_total_deadline_closes_continuously_streaming_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowStream(httpx.AsyncByteStream):
        closed = False
        produced = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                await asyncio.sleep(0.005)
                self.produced += 1
                yield b"still streaming\n"

        async def aclose(self) -> None:
            self.closed = True

    stream = SlowStream()
    monkeypatch.setattr(web_tools, "_FETCH_TIMEOUT_SECONDS", 0.04)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, stream=stream),
    )) as client:
        with pytest.raises(RuntimeError, match="抓取网页失败") as caught:
            await web_tools._fetch_page(client, "https://example.com/slow")
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert stream.closed is True
    assert stream.produced < 100


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failure", "cancel"])
async def test_batch_fetch_closes_other_streams_before_leaving(
    monkeypatch: pytest.MonkeyPatch, mode: str, tool_context: MagicMock,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class WaitingStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            started.set()
            await release.wait()
            yield b"done"

        async def aclose(self) -> None:
            self.closed = True

    stream = WaitingStream()

    async def handle(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fail":
            await started.wait()
            return httpx.Response(503)
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=stream)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    urls = ["https://example.com/slow"]
    if mode == "failure":
        urls.append("https://example.com/fail")
    task = asyncio.create_task(web_tools.create_fetch_webpage_tool(tool_context).ainvoke({"urls": urls}))
    try:
        await started.wait()
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError, match="抓取网页失败") as caught:
                await task
            assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
        assert stream.closed is True
    finally:
        release.set()


@pytest.mark.asyncio
async def test_fetch_webpage_rejects_removed_query_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_client(**_kwargs: object) -> None:
        raise AssertionError("已移除的参数应在创建 HTTP 客户端前被拒绝")

    tool = web_tools.create_fetch_webpage_tool(MagicMock())
    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ValidationError) as caught:
        await tool.ainvoke({"urls": ["https://example.com"], "query": "unused"})
    assert caught.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("tool_name", ["web_search", "fetch_webpage"])
@pytest.mark.parametrize("location", ["/private/web-library-cache", r"C:\private\web-library-cache", r"\\target\private\web-library-cache"])
async def test_web_dependency_failure_preserves_cause_without_publishing_native_locations(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tool_name: str, location: str,
    tool_context: MagicMock,
) -> None:
    native = PermissionError(13, "外部库无法读取本机文件: " + location, location)
    url = "https://web-failure.invalid/document"
    dependency_error = httpx.ConnectError(str(native), request=httpx.Request("GET", url)) if tool_name == "fetch_webpage" else native
    observed: list[str] = []

    def search(query: str, **_kwargs: object) -> list[dict[str, str]]:
        observed.append(query)
        raise dependency_error

    handle = httpx.AsyncHTTPTransport.handle_async_request

    async def fetch(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        if str(request.url) == url:
            observed.append(str(request.url))
            raise dependency_error from native
        return await handle(transport, request)

    monkeypatch.setattr(web_tools, "search_web", search)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fetch)
    context = tool_context
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
