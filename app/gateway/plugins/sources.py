"""显式自有来源目录；无配置时不请求网络，不维护市场镜像。"""

from __future__ import annotations

import json
from typing import cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.gateway.plugin_installation import (
    PluginSourceArtifact,
    PluginSourceCatalogDTO,
    PluginSourceCatalogRequest,
    PluginSourceConfig,
    PluginSourceStatusDTO,
)

_MAX_CATALOG_BYTES = 2 * 1024 * 1024


def plugin_sources(payload: dict[str, object]) -> list[PluginSourceConfig]:
    plugins = cast(dict[str, object], payload.get("plugins", {}))
    sources = cast(dict[str, object], plugins.get("sources", {}))
    entries = [PluginSourceConfig.model_validate(value) for value in cast(list[object], sources.get("entries", []))]
    if len({source.source_id for source in entries}) != len(entries):
        raise ValueError("插件来源 source_id 不能重复")
    return entries


def source_status(payload: dict[str, object]) -> PluginSourceStatusDTO:
    sources = plugin_sources(payload)
    configured = any(source.enabled for source in sources)
    return PluginSourceStatusDTO(configured=configured, entries=sources,
        message="已配置自有插件来源" if configured else "插件源尚未配置")


def enabled_source(payload: dict[str, object], source_id: str) -> PluginSourceConfig:
    source = next((source for source in plugin_sources(payload) if source.source_id == source_id), None)
    if source is None or not source.enabled:
        raise ValueError(f"插件来源未配置或未启用: {source_id}")
    return source


class _CatalogPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol_version: int
    source_id: str
    catalog_revision: str = Field(min_length=1)
    items: list[PluginSourceArtifact] = Field(max_length=100)
    next_cursor: str | None = None


async def read_catalog(client: httpx.AsyncClient, *, gateway_id: str, source: PluginSourceConfig,
                       request: PluginSourceCatalogRequest) -> PluginSourceCatalogDTO:
    assert source.base_url is not None
    params = {"query": request.query}
    if request.cursor is not None:
        params["cursor"] = request.cursor
    async with client.stream("GET", source.base_url.rstrip("/") + "/catalog", params=params) as response:
        response.raise_for_status()
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > _MAX_CATALOG_BYTES:
                raise ValueError("插件目录响应超过 2 MiB")
    try:
        page = _CatalogPage.model_validate(json.loads(content))
    except ValidationError as error:
        raise ValueError("插件目录字段非法: " + "; ".join(
            issue["msg"] for issue in error.errors(include_input=False, include_url=False))) from error
    if page.protocol_version != 1 or page.source_id != source.source_id:
        raise ValueError("插件目录协议版本或来源身份不匹配")
    return PluginSourceCatalogDTO(gateway_id=gateway_id, **page.model_dump())
