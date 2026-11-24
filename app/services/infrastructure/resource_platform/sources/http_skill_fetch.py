"""HTTP 获取能力：只处理传输和条件请求，接受事实由统一来源 owner 发布。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import Literal

import httpx
from pydantic import Field, field_validator

from app.runtime.network_proxy import ProxyRoutes
from app.services.infrastructure.resource_platform.sources.skill_fetch_contracts import (
    SkillFetchConfig,
    SkillFetchError,
    SkillFetchResult,
)


class HttpSkillFetchConfig(SkillFetchConfig, frozen=True):
    protocol: Literal["http"]
    url: str = Field(min_length=1)
    timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)

    @field_validator("url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        try:
            parsed = httpx.URL(value)
        except httpx.InvalidURL as error:
            raise ValueError("HTTP Skill 获取地址不是有效 URL") from error
        if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.fragment:
            raise ValueError("HTTP Skill 获取地址必须是无 fragment 的绝对 HTTP(S) URL")
        return value


class HttpSkillFetch:
    def __init__(self, client: httpx.AsyncClient | None = None, *,
                 routes_provider: Callable[[], Awaitable[ProxyRoutes]] | None = None) -> None:
        if client is not None and routes_provider is not None:
            raise ValueError("HTTP Skill 只能绑定一个客户端 owner")
        self._owns_client = client is None and routes_provider is None
        self._client = httpx.AsyncClient(trust_env=False) if self._owns_client else client
        self._routes_provider = routes_provider

    @asynccontextmanager
    async def _fetch_client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
            return
        assert self._routes_provider is not None
        routes = await self._routes_provider()
        verify = httpx.create_ssl_context(trust_env=True)
        # 每次有界获取持有自己的客户端；热切换不会关闭尚在下载的旧响应。
        async with httpx.AsyncClient(trust_env=False, verify=verify, mounts=routes.async_mounts(verify=verify)) as client:
            yield client

    async def fetch(
        self, config: SkillFetchConfig, *, validators: Mapping[str, str],
        has_snapshot: bool, max_bytes: int,
    ) -> SkillFetchResult:
        if not isinstance(config, HttpSkillFetchConfig):
            raise TypeError("HTTP 获取能力未绑定对应的 typed 配置")
        headers = {}
        for key, header in (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")):
            if key in validators:
                headers[header] = validators[key]
        try:
            async with (
                asyncio.timeout(config.timeout_seconds), self._fetch_client() as client,
                client.stream("GET", config.url, headers=headers, timeout=config.timeout_seconds,
                              follow_redirects=True) as response,
            ):
                if response.status_code == 304:
                    if not has_snapshot or not headers:
                        raise ValueError("HTTP Skill 304 缺少已接受正文或条件请求")
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            raise ValueError("HTTP Skill 304 不得包含正文")
                    cache = dict(validators)
                    body = None
                else:
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise ValueError("HTTP Skill 获取只接受完整 200 或条件 304")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > max_bytes:
                            raise ValueError("HTTP Skill 正文超过来源字节上限")
                        data.extend(chunk)
                    body, cache = bytes(data), {}
                for key, header in (("etag", "ETag"), ("last_modified", "Last-Modified")):
                    if header in response.headers:
                        cache[key] = response.headers[header]
                return SkillFetchResult(body, MappingProxyType(cache))
        except httpx.HTTPError as error:
            raise SkillFetchError(f"HTTP Skill 获取失败: {type(error).__name__}") from error

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
