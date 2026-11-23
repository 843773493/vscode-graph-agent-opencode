"""构造由模型实例持有的协议 HTTP client；只消费 owner 已准备的代理路由。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx

from app.runtime.litellm_runtime import AsyncHTTPHandler, HTTPHandler
from app.runtime.model_stream import (
    model_stream_async_transport,
    model_stream_sync_transport,
)
from app.runtime.network_proxy import ProxyRoutes


class _NoAuthentication(httpx.Auth):
    """显式无认证在发送边界移除 LiteLLM 从环境补入的认证头。"""

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        request.headers.pop("Authorization", None)
        request.headers.pop("X-Api-Key", None)
        yield request


class _OmitEmptyAuthorization(httpx.Auth):
    """SDK 用空认证头声明匿名请求；实际发送时移除，保留显式配置的认证头。"""

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        if request.headers.get("Authorization") == "":
            del request.headers["Authorization"]
        yield request


class _ProviderAsyncHTTPHandler(AsyncHTTPHandler):
    """沿 SDK 创建钩子固定策略；短连接重试也不改读隐式环境。"""

    def __init__(self, factory: Callable[[], httpx.AsyncClient]) -> None:
        self._provider_client_factory = factory
        super().__init__()
        # 与 SDK 的外部 client setter 保持同一所有权；驻留池只由模型 owner 关闭。
        self._owns_client = False

    def create_client(self, timeout: float | httpx.Timeout | None = None, **_kwargs: object) -> httpx.AsyncClient:
        client = self._provider_client_factory()
        if timeout is not None:
            client.timeout = httpx.Timeout(timeout)
        return client


def _async_client_factory(*, routes: ProxyRoutes, auth: httpx.Auth | None) -> Callable[[], httpx.AsyncClient]:
    verify = httpx.create_ssl_context(trust_env=True)

    def create_client() -> httpx.AsyncClient:
        # 默认出口及各代理出口都经过同一录放 owner；重试各自持有新池。
        transport = model_stream_async_transport(lambda: httpx.AsyncHTTPTransport(verify=verify, trust_env=False))
        mounts = {
            pattern: None if proxy is None else model_stream_async_transport(
                lambda proxy=proxy: httpx.AsyncHTTPTransport(proxy=proxy, verify=verify, trust_env=False),
            ) for pattern, proxy in routes.routes
        }
        return httpx.AsyncClient(transport=transport, trust_env=False, verify=verify, mounts=mounts, auth=auth)

    return create_client


def _sync_client(*, routes: ProxyRoutes, auth: httpx.Auth | None) -> httpx.Client:
    verify = httpx.create_ssl_context(trust_env=True)
    transport = model_stream_sync_transport(lambda: httpx.HTTPTransport(verify=verify, trust_env=False))
    mounts = {
        pattern: None if proxy is None else model_stream_sync_transport(
            lambda proxy=proxy: httpx.HTTPTransport(proxy=proxy, verify=verify, trust_env=False),
        ) for pattern, proxy in routes.routes
    }
    return httpx.Client(transport=transport, trust_env=False, verify=verify, mounts=mounts, auth=auth)


def build_provider_http_handler(*, is_async: bool, routes: ProxyRoutes, no_auth: bool) -> AsyncHTTPHandler | HTTPHandler:
    """Anthropic 和 Responses 使用 LiteLLM handler，外部注入的 httpx client 由调用方关闭。"""
    if is_async:
        return _ProviderAsyncHTTPHandler(_async_client_factory(routes=routes, auth=_NoAuthentication() if no_auth else None))
    return HTTPHandler(client=_sync_client(routes=routes, auth=_NoAuthentication() if no_auth else None))


def build_provider_openai_client(
    *,
    is_async: bool,
    base_url: str | None,
    api_key: str | None,
    routes: ProxyRoutes,
) -> Any:
    """Chat Completions 使用 OpenAI SDK client，并沿其 close 关闭连接池。"""
    from openai import AsyncOpenAI, OpenAI

    auth = _OmitEmptyAuthorization() if api_key == "" else None
    client_type = AsyncOpenAI if is_async else OpenAI
    return client_type(
        api_key=api_key,
        _enforce_credentials=api_key != "",
        default_headers={"Authorization": ""} if api_key == "" else None,
        base_url=base_url,
        http_client=_async_client_factory(routes=routes, auth=auth)() if is_async else _sync_client(routes=routes, auth=auth),
    )


__all__ = [
    "build_provider_http_handler",
    "build_provider_openai_client",
]
