"""受控真实 socket 验证 Provider 客户端路由，不能替代完整产品 live 验收。"""

from __future__ import annotations

import json
import socket
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app.agents.providers.provider_http_client import build_provider_openai_client
from app.runtime.model_stream import ModelStreamTransportController
from app.runtime.network_proxy import ProxyRoutes
from app.runtime.provider_model import build_model_from_provider
from app.testing.model_stream import (
    get_protocol_codec,
    load_cassette,
    load_model_stream_config,
    load_scenario,
)
from configs.network import DirectProxy, EnvironmentProxy, ExplicitProxy
from tests.support.http_server import running_http_server


@pytest.fixture
def proxy_endpoint():
    requests = []
    stream_payloads = {}
    for suffix, scenario_id in (("/responses", "responses-basic-text"), ("/messages", "anthropic-reasoning-stream")):
        scenario = load_scenario(Path.cwd() / "tests/fixtures/model_stream", scenario_id)
        interaction = scenario.cassette.interactions[0]
        codec = get_protocol_codec(interaction.protocol)
        stream_payloads[suffix] = b"".join(codec.encode(frame) for frame in interaction.response.frames)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.headers.get("Proxy-Authorization")))
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            streaming = body.get("stream", False)
            if streaming:
                payload = next((value for suffix, value in stream_payloads.items() if self.path.endswith(suffix)),
                               b'data: {"id":"recorded","object":"chat.completion.chunk","created":1,"model":"test-model","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\ndata: [DONE]\n\n')
            elif self.path.endswith("/responses"):
                payload = json.dumps({
                    "id": "resp-test", "object": "response", "created_at": 1, "model": "test-model", "status": "completed",
                    "output": [{"id": "msg-test", "type": "message", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }).encode()
            elif self.path.endswith("/messages"):
                payload = json.dumps({
                    "id": "msg-test", "type": "message", "role": "assistant", "model": "test-model",
                    "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }).encode()
            else:
                payload = json.dumps({
                    "id": "chat-test", "object": "chat.completion", "created": 1, "model": "test-model",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            requests.append((self.path, self.headers.get("Proxy-Authorization")))
            payload = b'{"data":[{"id":"upstream-model"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *args):
            pass

    with running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as server:
        yield f"http://127.0.0.1:{server.server_address[1]}", requests


@pytest.fixture
def proxy_environment(monkeypatch: pytest.MonkeyPatch, proxy_endpoint):
    endpoint, requests = proxy_endpoint
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, endpoint)
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
    return requests


@pytest.mark.asyncio
async def test_async_provider_catalog_uses_captured_proxy(proxy_environment):
    client = build_provider_openai_client(is_async=True, base_url="http://upstream.invalid/v1", api_key="", routes=ProxyRoutes.from_configuration(EnvironmentProxy()))
    try:
        models = await client.models.list(timeout=2)
        assert [model.id for model in models.data] == ["upstream-model"]
        assert proxy_environment == [("http://upstream.invalid/v1/models", None)]
    finally:
        await client.close()


def test_sync_provider_catalog_uses_captured_proxy(proxy_environment):
    with build_provider_openai_client(is_async=False, base_url="http://upstream.invalid/v1", api_key="", routes=ProxyRoutes.from_configuration(EnvironmentProxy())) as client:
        models = client.models.list(timeout=2)
    assert [model.id for model in models.data] == ["upstream-model"]
    assert proxy_environment == [("http://upstream.invalid/v1/models", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_explicit_proxy_ignores_environment_bypass_and_uses_prepared_auth(
    proxy_environment, proxy_endpoint, monkeypatch: pytest.MonkeyPatch, asynchronous: bool,
):
    endpoint, requests = proxy_endpoint
    config = ExplicitProxy.model_validate({"url": endpoint, "auth": {
        "username": "test", "password": "secret:sec_0123456789abcdef0123456789abcdef"}})
    routes = ProxyRoutes.from_configuration(config, password="test-password", environment={"NO_PROXY": "*"})
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://unreachable.invalid:1")
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, "*")
    client = build_provider_openai_client(
        is_async=asynchronous, base_url="http://upstream.invalid/v1", api_key="", routes=routes,
    )
    try:
        result = client.models.list(timeout=2)
        models = await result if asynchronous else result
        assert [model.id for model in models.data] == ["upstream-model"]
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()
    assert requests == [("http://upstream.invalid/v1/models", "Basic dGVzdDp0ZXN0LXBhc3N3b3Jk")]


@pytest.mark.asyncio
async def test_direct_policy_does_not_use_environment_proxy(proxy_environment, proxy_endpoint):
    endpoint, requests = proxy_endpoint
    client = build_provider_openai_client(is_async=True, base_url=endpoint + "/v1", api_key="", routes=ProxyRoutes.from_configuration(DirectProxy()))
    try:
        models = await client.models.list(timeout=2)
        assert [model.id for model in models.data] == ["upstream-model"]
        assert requests == [("/v1/models", None)]
    finally:
        await client.close()


def test_unreachable_explicit_proxy_does_not_fall_back_to_reachable_upstream(proxy_endpoint):
    endpoint, requests = proxy_endpoint
    # 绑定但不 listen，固定本次拒绝连接的地址，避免借用可能被其他进程抢占的空闲端口。
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        routes = ProxyRoutes.from_configuration(ExplicitProxy(url=f"http://127.0.0.1:{unavailable.getsockname()[1]}"))
        verify = httpx.create_ssl_context(trust_env=False)
        with (
            httpx.Client(trust_env=False, verify=verify, mounts=routes.sync_mounts(verify=verify)) as client,
            pytest.raises(httpx.ConnectError),
        ):
            client.get(endpoint + "/v1/models", timeout=0.5)
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_record_wraps_the_actual_proxy_route_and_keeps_owner_alive(proxy_environment, tmp_path: Path, asynchronous: bool):
    """真实代理捕获与录制产物必须对应同一次请求，不允许 record 偷走直连出口。"""
    config = load_model_stream_config(Path.cwd() / "configs/tests/model_stream/model_stream_chat_basic.jsonc")
    config = replace(config, transport=replace(config.transport, mode="record", artifact_root=tmp_path))
    controller = ModelStreamTransportController.configure(config)
    assert controller is not None
    clients = []
    try:
        for _ in range(2):
            client = build_provider_openai_client(
                is_async=asynchronous, base_url="http://upstream.invalid/v1", api_key="test-key", routes=ProxyRoutes.from_configuration(EnvironmentProxy()),
            )
            clients.append(client)
            result = client.chat.completions.create(
                model="test-model", messages=[{"role": "user", "content": "hi"}], stream=True, timeout=2,
            )
            stream = await result if asynchronous else result
            content = [chunk.choices[0].delta.content async for chunk in stream] if asynchronous else [chunk.choices[0].delta.content for chunk in stream]
            assert content == ["ok"]
            if asynchronous:
                await client.close()
            else:
                client.close()
            assert controller.installed
        assert proxy_environment == [("http://upstream.invalid/v1/chat/completions", None)] * 2
        assert controller.transport.call_count == 2
        artifacts = list(tmp_path.glob("basic-text-*.json"))
        assert len(artifacts) == 2
        for artifact in artifacts:
            cassette = load_cassette(artifact)
            assert cassette.metadata["source"] == "recorded"
            assert cassette.interactions[0].request.url == "http://upstream.invalid/v1/chat/completions"
            assert "test-key" not in artifact.read_text(encoding="utf-8")
    finally:
        for client in clients:
            if asynchronous:
                await client.close()
            else:
                client.close()
        await controller.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("protocol,path,adapter", [
    ("chat_completions", "/v1/chat/completions", "openai"),
    ("chat_completions", "/v1/chat/completions", "openrouter"),
    ("chat_completions", "/v1/chat/completions", "deepseek"),
    ("responses", "/v1/responses", "openai"),
    ("anthropic_messages", "/v1/messages", "anthropic"),
])
async def test_authenticated_model_requests_use_instance_proxy_without_model_test_exemption(
    proxy_environment, monkeypatch: pytest.MonkeyPatch, protocol: str, path: str, adapter: str, asynchronous: bool,
):
    """经过真实模型构建和 SDK；此用例不替代正式 Job 的封存验收。"""
    model = build_model_from_provider(
        provider={
            "provider_id": "test", "model_id": "test", "model": "test-model",
            "endpoint": "http://upstream.invalid" + ("" if adapter == "anthropic" else "/v1"),
            "api_key": "test-key", "custom_llm_provider": adapter,
            "api_mode": {"protocol": protocol, "streaming": False, "model_info": {}, "supports_reasoning": {}},
        },
        runtime_config={}, invocation_purpose="tool_test",
    )
    try:
        response = await model.ainvoke("hi") if asynchronous else model.invoke("hi")
        assert response.text == "ok"
        assert proxy_environment == [("http://upstream.invalid" + path, None)]
        assert (model._owned_async_client if asynchronous else model._owned_sync_client) is not None
        # 同一模型的另一种池不能因进程环境后来改变而换出口。
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(key, "http://unreachable.invalid:1")
        for key in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(key, "*")
        other = model.invoke("hi") if asynchronous else await model.ainvoke("hi")
        assert other.text == "ok"
        assert proxy_environment == [("http://upstream.invalid" + path, None)] * 2
    finally:
        await model.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("protocol,adapter,scenario_id,suffix,expected", [
    ("chat_completions", "openrouter", "basic-text", "/v1/chat/completions", "ok"),
    ("chat_completions", "deepseek", "basic-text", "/v1/chat/completions", "ok"),
    ("responses", "openai", "responses-basic-text", "/v1/responses", "响应文本"),
    ("anthropic_messages", "anthropic", "anthropic-reasoning-stream", "/v1/messages", "Anthropic Messages 验证结论。"),
])
async def test_actual_adapters_record_the_selected_proxy_route(
    proxy_environment, tmp_path: Path, asynchronous: bool,
    protocol: str, adapter: str, scenario_id: str, suffix: str, expected: str,
):
    config = load_model_stream_config(Path.cwd() / "configs/tests/model_stream/model_stream_chat_basic.jsonc")
    config = replace(config, transport=replace(config.transport, mode="record", scenario_id=scenario_id, artifact_root=tmp_path))
    controller = ModelStreamTransportController.configure(config)
    assert controller is not None
    model = None
    try:
        model = build_model_from_provider(provider={
            "provider_id": "test", "model_id": "test", "model": "test-model",
            "endpoint": "http://upstream.invalid" + ("" if adapter == "anthropic" else "/v1"),
            "api_key": "test-key", "custom_llm_provider": adapter,
            "api_mode": {"protocol": protocol, "streaming": True, "model_info": {}, "supports_reasoning": {}},
        }, runtime_config={}, invocation_purpose="tool_test")
        text = "".join([chunk.text async for chunk in model.astream("hi")]) if asynchronous else "".join(chunk.text for chunk in model.stream("hi"))
        assert expected in text
        await model.aclose()
        assert controller.installed
        assert proxy_environment == [("http://upstream.invalid" + suffix, None)]
        assert controller.transport.call_count == 1
        assert controller.transport.incomplete_artifacts == ()
        artifacts = list(tmp_path.glob("*.json"))
        assert len(artifacts) == 1
        cassette = load_cassette(artifacts[0])
        assert cassette.interactions[0].request.url == "http://upstream.invalid" + suffix
        assert "test-key" not in artifacts[0].read_text(encoding="utf-8")
    finally:
        if model is not None:
            await model.aclose()
        await controller.aclose()
