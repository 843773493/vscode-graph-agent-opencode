"""构造由模型实例持有的协议 HTTP client；按原配置决定是否读取环境代理。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from app.runtime.litellm_runtime import AsyncHTTPHandler, HTTPHandler
from app.runtime.model_stream import model_stream_async_transport


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


def build_provider_http_handler(*, is_async: bool, trust_env: bool, no_auth: bool) -> AsyncHTTPHandler | HTTPHandler:
    """Anthropic 和 Responses 使用 LiteLLM handler，外部注入的 httpx client 由调用方关闭。"""
    if is_async:
        handler = AsyncHTTPHandler()
        handler.client = httpx.AsyncClient(
            transport=model_stream_async_transport(), trust_env=trust_env,
            auth=_NoAuthentication() if no_auth else None,
        )
        return handler
    return HTTPHandler(client=httpx.Client(trust_env=trust_env, auth=_NoAuthentication() if no_auth else None))


def build_provider_openai_client(
    *,
    is_async: bool,
    base_url: str | None,
    api_key: str | None,
    trust_env: bool,
) -> Any:
    """Chat Completions 使用 OpenAI SDK client，并沿其 close 关闭连接池。"""
    if is_async:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=api_key,
            _enforce_credentials=api_key != "",
            default_headers={"Authorization": ""} if api_key == "" else None,
            base_url=base_url,
            http_client=httpx.AsyncClient(
                transport=model_stream_async_transport(), trust_env=trust_env,
                auth=_OmitEmptyAuthorization() if api_key == "" else None,
            ),
        )
    from openai import OpenAI

    return OpenAI(
        api_key=api_key,
        _enforce_credentials=api_key != "",
        default_headers={"Authorization": ""} if api_key == "" else None,
        base_url=base_url,
        http_client=httpx.Client(trust_env=trust_env, auth=_OmitEmptyAuthorization() if api_key == "" else None),
    )


__all__ = [
    "build_provider_http_handler",
    "build_provider_openai_client",
]
