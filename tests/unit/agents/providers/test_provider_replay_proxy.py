"""环境代理存在时，Provider 仍必须先经过已安装的录放 transport。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.agents.providers import provider_http_client
from app.runtime.model_stream import ModelStreamTransportController
from app.runtime.network_proxy import ProxyRoutes
from app.runtime.provider_model import build_model_from_provider
from app.testing.model_stream import (
    load_cassette_from_object,
    load_model_stream_config,
    load_scenario,
)
from configs.network import DirectProxy, EnvironmentProxy


@pytest.fixture
def replay_requests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://proxy.invalid")
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    requests: list[str] = []

    def replay(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json={"replayed": True})

    async def unexpected_network(_transport, request: httpx.Request):
        raise AssertionError(f"回放不得连接真实网络: {request.url.host}")

    monkeypatch.setattr(provider_http_client, "model_stream_async_transport", lambda *_: httpx.MockTransport(replay))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", unexpected_network)
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["handler", "openai"])
async def test_provider_replay_cannot_be_bypassed_by_environment_proxy(adapter: str, replay_requests: list[str]):
    if adapter == "handler":
        owner = provider_http_client.build_provider_http_handler(is_async=True, routes=ProxyRoutes.from_configuration(EnvironmentProxy()), no_auth=True)
        client = owner.client
    else:
        owner = provider_http_client.build_provider_openai_client(
            is_async=True, base_url="https://model.invalid/v1", api_key="", routes=ProxyRoutes.from_configuration(EnvironmentProxy()),
        )
        client = owner._client
    try:
        response = await client.post("https://model.invalid/v1/chat/completions", json={"stream": False})
        assert response.json() == {"replayed": True}
        assert replay_requests == ["https://model.invalid/v1/chat/completions"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_async_handler_never_creates_an_sdk_environment_client(monkeypatch: pytest.MonkeyPatch):
    def forbidden_sdk_client(*_args: object, **_kwargs: object):
        raise AssertionError("不得先初始化 SDK 的隐式环境客户端")

    monkeypatch.setattr(provider_http_client.AsyncHTTPHandler, "create_client", forbidden_sdk_client)
    handler = provider_http_client.build_provider_http_handler(is_async=True, routes=ProxyRoutes.from_configuration(DirectProxy()), no_auth=True)
    try:
        assert isinstance(handler.client, httpx.AsyncClient)
        await handler.close()
        assert not handler.client.is_closed
    finally:
        await handler.client.aclose()


@pytest.mark.asyncio
async def test_sdk_short_connection_retry_does_not_close_the_resident_client(monkeypatch: pytest.MonkeyPatch):
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("controlled connect failure", request=request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(provider_http_client, "model_stream_async_transport", lambda *_: httpx.MockTransport(respond))
    handler = provider_http_client.build_provider_http_handler(is_async=True, routes=ProxyRoutes.from_configuration(EnvironmentProxy()), no_auth=True)
    resident = handler.client
    try:
        assert (await handler.post("https://model.invalid/test", json={})).json() == {"ok": True}
        assert not resident.is_closed
        assert handler.client is resident
        assert (await handler.post("https://model.invalid/test", json={})).json() == {"ok": True}
        assert calls == 3
    finally:
        await resident.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["openai", "anthropic"])
@pytest.mark.parametrize("failing_pool", ["async", "sync"])
async def test_failed_model_pool_close_keeps_retry_and_releases_the_other_pool(
    monkeypatch: pytest.MonkeyPatch, adapter: str, failing_pool: str,
):
    model = build_model_from_provider(provider={
        "provider_id": "test", "model_id": "test", "model": "test-model",
        "endpoint": "http://model.invalid/v1", "api_key": "test-key", "custom_llm_provider": adapter,
        "api_mode": {"protocol": "anthropic_messages" if adapter == "anthropic" else "chat_completions",
                     "model_info": {}, "supports_reasoning": {}},
        "request_options": {"no_proxy": True},
    }, runtime_config={}, invocation_purpose="tool_test")
    async_owner = model._provider_http_client(is_async=True)
    sync_owner = model._provider_http_client(is_async=False)
    assert model._provider_http_client(is_async=True) is async_owner
    assert model._provider_http_client(is_async=False) is sync_owner
    async_pool = async_owner.client if adapter == "anthropic" else async_owner._client
    sync_pool = sync_owner.client if adapter == "anthropic" else sync_owner._client
    close_async, close_sync = async_pool.aclose, sync_pool.close
    failures = 0

    async def close_async_once():
        nonlocal failures
        if failing_pool == "async" and failures == 0:
            failures += 1
            raise OSError("controlled async pool close failure")
        await close_async()

    def close_sync_once():
        nonlocal failures
        if failing_pool == "sync" and failures == 0:
            failures += 1
            raise OSError("controlled sync pool close failure")
        close_sync()

    monkeypatch.setattr(async_pool, "aclose", close_async_once)
    monkeypatch.setattr(sync_pool, "close", close_sync_once)
    try:
        with pytest.raises(OSError, match=f"controlled {failing_pool}"):
            await model.aclose()
        assert model._owned_async_client is (async_owner if failing_pool == "async" else None)
        assert model._owned_sync_client is (sync_owner if failing_pool == "sync" else None)
        assert async_pool.is_closed is (failing_pool != "async")
        assert sync_pool.is_closed is (failing_pool != "sync")
    finally:
        await model.aclose()
    assert async_pool.is_closed and sync_pool.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("scenario_id,protocol,suffix,expected,adapter", [
    ("basic-text", "chat_completions", "/chat/completions", "基础回放", "openai"),
    ("basic-text", "chat_completions", "/chat/completions", "基础回放", "openrouter"),
    ("basic-text", "chat_completions", "/chat/completions", "基础回放", "deepseek"),
    ("responses-basic-text", "responses", "/responses", "响应文本", "openai"),
    ("anthropic-reasoning-stream", "anthropic_messages", "/v1/messages", "Anthropic Messages 验证结论。", "anthropic"),
])
async def test_actual_adapters_replay_without_creating_live_transports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    asynchronous: bool, scenario_id: str, protocol: str, suffix: str, expected: str, adapter: str,
):
    """真实构建、SDK 解码和流消费；所有代理／上游 transport 的创建都禁止。"""
    def forbidden_transport(*_args, **_kwargs):
        raise AssertionError("replay 不得创建真实 HTTP transport")

    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://proxy.invalid")
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    config = load_model_stream_config(Path.cwd() / "configs/tests/model_stream/model_stream_chat_basic.jsonc")
    config = replace(config, transport=replace(config.transport, scenario_id=scenario_id, artifact_root=tmp_path))
    scenario = load_scenario(config.transport.fixture_root, scenario_id)
    if adapter == "openrouter":
        # OpenRouter 原生模型名含提供方前缀；沿原严格匹配生成本例资产，不改共享夹具。
        raw = deepcopy(scenario.cassette.raw)
        raw["interactions"][0]["request"]["match"]["model"] = "example/big-pickle"
        scenario = replace(scenario, cassette=load_cassette_from_object(raw))
        monkeypatch.setattr("app.runtime.model_stream.load_scenario", lambda *_: scenario)
    request = scenario.cassette.interactions[0].request
    controller = ModelStreamTransportController.configure(config)
    assert controller is not None
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", forbidden_transport)
    monkeypatch.setattr(httpx, "HTTPTransport", forbidden_transport)
    model = None
    try:
        model = build_model_from_provider(
            provider={
                "provider_id": "test", "model_id": "test", "model": request.match["model"],
                "endpoint": request.url.removesuffix(suffix), "api_key": "test-key",
                "custom_llm_provider": adapter,
                "api_mode": {"protocol": protocol, "model_info": {}, "supports_reasoning": {}},
            }, runtime_config={}, invocation_purpose="tool_test",
        )
        if asynchronous:
            text = "".join([chunk.text async for chunk in model.astream("hi")])
        else:
            text = "".join(chunk.text for chunk in model.stream("hi"))
        assert expected in text
        assert controller.transport.request_urls == (request.url,)
        assert controller.transport.hit_counts == {0: 1}
    finally:
        if model is not None:
            await model.aclose()
        await controller.aclose()
