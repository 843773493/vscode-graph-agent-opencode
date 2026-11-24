"""OAuth 刷新经真实代理，驻留模型逐次交付凭据给 SDK；不访问外部认证服务。"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app.core.failures import public_error
from app.runtime.chatgpt_auth import prepare_chatgpt_oauth
from app.runtime.model_stream import ModelStreamTransportController
from app.runtime.network_proxy import ProxyRoutes
from app.runtime.provider_model import build_model_from_provider
from app.testing.model_stream import (
    get_protocol_codec,
    load_model_stream_config,
    load_scenario,
)
from configs.network import ExplicitProxy
from tests.support.http_server import running_http_server


def _token(expiry: int, account_id: str = "oauth-account") -> str:
    claims = {"exp": expiry, "https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


@pytest.fixture
def oauth_refresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from app.runtime.litellm_runtime import load_litellm

    load_litellm()
    from litellm.llms.chatgpt import authenticator, common_utils

    token_directory = tmp_path / "auth"
    token_directory.mkdir()
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(token_directory))
    monkeypatch.delenv("CHATGPT_AUTH_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    requests = []
    tokens = [_token(int(time.time()) + 3600 + index) for index in range(3)]

    def expire():
        (token_directory / "auth.json").write_text(json.dumps({
            "access_token": _token(1), "expires_at": 1,
            "refresh_token": "refresh-secret", "account_id": "oauth-account",
        }))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Proxy-Authorization"), body))
            payload = json.dumps({"access_token": tokens[len(requests) - 1], "id_token": tokens[0],
                                  "refresh_token": "rotated-refresh"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def forbidden(*_args, **_kwargs):
        pytest.fail("SDK 全局认证 owner 不应参与已准备的 OAuth 请求")

    monkeypatch.setattr(authenticator, "get_httpx_client", forbidden)
    monkeypatch.setattr(authenticator, "get_cached_authenticator", forbidden)
    monkeypatch.setattr(authenticator.Authenticator, "get_access_token", forbidden)
    monkeypatch.setattr(authenticator.Authenticator, "get_account_id", forbidden)
    monkeypatch.setattr(common_utils, "CHATGPT_OAUTH_TOKEN_URL", "http://oauth.invalid/token")
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    with running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as server:
        routes = ProxyRoutes.from_configuration(ExplicitProxy.model_validate({
            "mode": "explicit", "url": f"http://127.0.0.1:{server.server_port}",
            "auth": {"username": "proxy-user", "password": "secret:sec_" + "1" * 32},
        }), password="proxy-secret")
        expire()
        yield token_directory, routes, tokens, requests, expire


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_resident_model_refreshes_via_provider_route_and_bypasses_sdk_authenticator(oauth_refresh, asynchronous):
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    from app.agents.providers.provider_http_client import _ProviderAsyncHTTPHandler

    _directory, routes, tokens, requests, expire = oauth_refresh
    model_routes = ProxyRoutes(())
    model = build_model_from_provider({
        "provider_id": "oauth", "model_id": "model", "model": "gpt-5.6-luna",
        "custom_llm_provider": "chatgpt", "endpoint": "https://chatgpt.com/backend-api/codex",
        "auth": {"type": "oauth", "method": "chatgpt"},
        "api_mode": {"protocol": "responses", "streaming": True, "model_info": {}, "supports_reasoning": {}},
        "proxy_routes": model_routes, "auth_proxy_routes": routes,
    }, {}, invocation_purpose="tool_test")
    scenario = load_scenario(Path.cwd() / "tests/fixtures/model_stream", "responses-basic-text")
    interaction = scenario.cassette.interactions[0]
    wire_content = b"".join(get_protocol_codec(interaction.protocol).encode(frame) for frame in interaction.response.frames)
    headers = []

    def upstream(request: httpx.Request):
        headers.append(dict(request.headers))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire_content)

    if asynchronous:
        model._owned_async_client = _ProviderAsyncHTTPHandler(
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream), trust_env=False),
        )
    else:
        model._owned_sync_client = HTTPHandler(client=httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False))
    try:
        for _index in range(2):
            content = "".join([chunk.text async for chunk in model.astream("hi")]) if asynchronous else "".join(chunk.text for chunk in model.stream("hi"))
            assert content
            expire()
        assert model.proxy_routes is model_routes
        assert model.auth_proxy_routes is routes
        assert [item["authorization"] for item in headers] == ["Bearer " + token for token in tokens[:2]]
        assert all(item["chatgpt-account-id"] == "oauth-account" for item in headers)
        assert len(requests) == 2
        assert all(url == "http://oauth.invalid/token" for url, _auth, _body in requests)
        assert all(auth == "Basic cHJveHktdXNlcjpwcm94eS1zZWNyZXQ=" for _url, auth, _body in requests)
        assert all(body["grant_type"] == "refresh_token" for _url, _auth, body in requests)
    finally:
        await model.aclose()


@pytest.mark.asyncio
async def test_refreshed_token_is_redacted_from_sdk_stream_failure(oauth_refresh):
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    _directory, routes, tokens, requests, expire = oauth_refresh
    model = build_model_from_provider({
        "provider_id": "oauth", "model_id": "model", "model": "gpt-5.6-luna",
        "custom_llm_provider": "chatgpt", "endpoint": "https://chatgpt.com/backend-api/codex",
        "auth": {"type": "oauth", "method": "chatgpt"},
        "api_mode": {"protocol": "responses", "streaming": True, "model_info": {}, "supports_reasoning": {}},
        "proxy_routes": ProxyRoutes(()), "auth_proxy_routes": routes,
    }, {}, invocation_purpose="tool_test")
    expire()
    def reject(request: httpx.Request):
        raise RuntimeError("upstream rejected " + request.headers["authorization"])
    model._owned_sync_client = HTTPHandler(client=httpx.Client(transport=httpx.MockTransport(reject), trust_env=False))
    try:
        with pytest.raises(Exception) as failure:
            list(model.stream("hi"))
        assert tokens[1] not in json.dumps(public_error(failure.value))
        assert len(requests) == 2
    finally:
        await model.aclose()


@pytest.mark.asyncio
async def test_replay_rejects_expired_oauth_without_creating_http_client(oauth_refresh, tmp_path: Path):
    directory, routes, _tokens, requests, _expire = oauth_refresh
    config = load_model_stream_config(Path.cwd() / "configs/tests/model_stream/model_stream_chat_basic.jsonc")
    controller = ModelStreamTransportController.configure(replace(config, transport=replace(
        config.transport, mode="replay", artifact_root=tmp_path / "artifacts",
    )))
    assert controller is not None
    try:
        with pytest.raises(RuntimeError, match="回放禁止刷新"):
            await asyncio.to_thread(prepare_chatgpt_oauth, directory, routes=routes)
        (directory / "auth.json").write_text(json.dumps({"access_token": _tokens[0], "account_id": "oauth-account"}))
        assert prepare_chatgpt_oauth(directory, routes=routes).access_token == _tokens[0]
        assert requests == []
        assert not controller.installed
    finally:
        await controller.aclose()
