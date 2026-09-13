"""按 Provider 配置构造不读取环境代理的 HTTP client。

LiteLLM 默认通过 httpx 的 ``trust_env=True`` 读取 ``HTTP_PROXY`` /
``HTTPS_PROXY`` / ``ALL_PROXY``。当工作区为了访问外网设置了全局代理时，
指向本地或内网 Sub2API 网关的 Provider 会被强制经过该代理并失败（502）。
这里提供显式的 Provider 级开关，让单个 Provider 绕过环境代理，而不是修改
全局 ``NO_PROXY``。
"""

from __future__ import annotations

from typing import Any

import httpx
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler


def _no_proxy_httpx_sync() -> httpx.Client:
    """构造不读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 的同步 httpx client。"""
    return httpx.Client(trust_env=False)


def _no_proxy_httpx_async() -> httpx.AsyncClient:
    """构造不读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 的异步 httpx client。"""
    return httpx.AsyncClient(trust_env=False)


def build_no_proxy_handler(*, is_async: bool) -> AsyncHTTPHandler | HTTPHandler:
    """构造直连（不读取环境代理）的 LiteLLM HTTP handler。

    Anthropic Messages 适配族直接使用 ``AsyncHTTPHandler``/``HTTPHandler``
    发起请求；把 ``trust_env=False`` 的 httpx client 注入后，该 Provider 的
    请求不会再经过环境代理。
    """
    if is_async:
        handler = AsyncHTTPHandler()
        handler.client = _no_proxy_httpx_async()
        return handler
    handler = HTTPHandler()
    handler.client = _no_proxy_httpx_sync()
    return handler


def build_no_proxy_openai_client(
    *,
    is_async: bool,
    base_url: str | None,
    api_key: str | None,
) -> Any:
    """构造直连（不读取环境代理）的 OpenAI SDK client。

    Chat Completions 与 Responses 适配族使用 OpenAI SDK client；注入带
    ``trust_env=False`` 的 http client 后即可绕过环境代理。
    """
    if is_async:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=_no_proxy_httpx_async(),
        )
    from openai import OpenAI

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=_no_proxy_httpx_sync(),
    )


__all__ = [
    "build_no_proxy_handler",
    "build_no_proxy_openai_client",
]
