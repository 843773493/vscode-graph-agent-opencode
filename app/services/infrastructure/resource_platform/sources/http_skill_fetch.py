"""HTTP 获取能力：只处理传输和条件请求，接受事实由统一来源 owner 发布。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

import httpx
from pydantic import Field, field_validator

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
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client if client is not None else httpx.AsyncClient(trust_env=False, follow_redirects=True)

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
            async with asyncio.timeout(config.timeout_seconds):
                async with self._client.stream("GET", config.url, headers=headers, timeout=config.timeout_seconds) as response:
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
        await self._client.aclose()
