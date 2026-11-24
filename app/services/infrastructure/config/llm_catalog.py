"""当前 Provider 的有界上游目录读取；LiteLLM 目录只补充能力，不代表账号授权。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx

from app.runtime.litellm_runtime import local_model_catalog
from app.runtime.model_metadata import model_metadata
from app.schemas.internal_v2.llm_probe import (
    LlmDiscoveredModelDTO,
    LlmModelCatalogDTO,
    LlmModelMetadataDTO,
    LlmProbeErrorDTO,
    LlmProviderDTO,
    LlmProvidersDTO,
)

CATALOG_MAX_BYTES = 2 * 1024 * 1024
CATALOG_MAX_MODELS = 5000


def discovered_model_metadata(
    provider: str, model: str, *, upstream: Mapping[str, Any], configured: Mapping[str, Any], owner_key: str | None = None,
) -> LlmModelMetadataDTO | None:
    """仅投影本次目录观察；显式配置优先，不保存或冒充已经实测的能力。"""
    fields = LlmModelMetadataDTO.model_fields.keys() - {"sources", "catalog_provider"}
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for source, candidate in (("litellm", model_metadata(provider, model, owner_key=owner_key) or {}), ("upstream", upstream), ("provider", configured)):
        # 只消费同名显式字段；总上下文 context_length 不能推导成输入容量。
        selected = {key: candidate[key] for key in fields if key in candidate and candidate[key] is not None}
        values.update(selected)
        sources.update(dict.fromkeys(selected, source))
    return LlmModelMetadataDTO.model_validate({**values, "sources": sources,
        "catalog_provider": provider if "litellm" in sources.values() else None}, strict=True) if values else None

# 这里只登记项目已提供的连接模板；目录中的其它 Provider 仍全部开放。
_PROVIDER_TEMPLATES = {
    "openai": ("https://api.openai.com/v1", "responses"),
    "anthropic": ("https://api.anthropic.com", "anthropic_messages"),
    "openrouter": ("https://openrouter.ai/api/v1", "chat_completions"),
    "deepseek": ("https://api.deepseek.com", "chat_completions"),
}


def list_catalog_providers(*, owner_key: str | None = None) -> LlmProvidersDTO:
    """直接枚举完整目录，不把目录收录等同于连接、认证或上游发现已适配。"""
    providers: dict[str, LlmProviderDTO] = {}
    for key, entry in local_model_catalog(owner_key=owner_key).items():
        if key in {"sample_spec", "fallback_generalizations"} or not isinstance(entry, Mapping):
            continue
        provider = entry.get("litellm_provider")
        if not isinstance(provider, str) or not provider:
            continue
        if provider not in providers:
            endpoint, protocol = _PROVIDER_TEMPLATES.get(provider, (None, None))
            providers[provider] = LlmProviderDTO(provider=provider, model_count=0, chat_model_count=0,
                default_endpoint=endpoint, protocol=protocol, upstream_discovery=provider in _PROVIDER_TEMPLATES)
        item = providers[provider]
        item.model_count += 1
        item.chat_model_count += entry.get("mode") in {"chat", "responses"}
    return LlmProvidersDTO(providers=sorted(providers.values(), key=lambda item: item.provider))


def list_catalog_models(provider: str, *, owner_key: str | None = None) -> LlmModelCatalogDTO:
    """目录名称仅作候选；用户仍可发现上游或手工输入未收录模型。"""
    models: dict[str, LlmDiscoveredModelDTO] = {}
    for key, entry in local_model_catalog(owner_key=owner_key).items():
        if not isinstance(entry, Mapping) or entry.get("litellm_provider") != provider or entry.get("mode") not in {"chat", "responses"}:
            continue
        model = key.removeprefix(provider + "/")
        models[model] = LlmDiscoveredModelDTO(model=model, metadata=discovered_model_metadata(provider, model, upstream={}, configured={}, owner_key=owner_key))
    return LlmModelCatalogDTO(provider=provider, models=sorted(models.values(), key=lambda item: item.model))


def public_endpoint(endpoint: str) -> str:
    return str(httpx.URL(endpoint).copy_with(username="", password="", query=None, fragment=None))


def model_catalog_url(connection: Mapping[str, Any]) -> httpx.URL:
    """发现与预检展示共用目录地址，不尝试其它候选路径。"""
    endpoint = httpx.URL(connection["endpoint"])
    if endpoint.scheme not in {"http", "https"} or not endpoint.host:
        raise ValueError("Provider endpoint 必须是 HTTP(S) 服务地址")
    path = endpoint.path.rstrip("/")
    if connection["api_mode"]["protocol"] == "anthropic_messages" and not path:
        path = "/v1"
    return endpoint.copy_with(path=path + "/models")


def probe_secret_values(connection: Mapping[str, Any]) -> tuple[str, ...]:
    """仅收集本次连接实际使用的秘密，供目录错误和测试输出共用。"""
    endpoint = httpx.URL(connection["endpoint"])
    headers = connection.get("request_options", {}).get("default_headers", {})
    values = {str(value) for value in (
        connection.get("api_key", ""), *headers.values(), endpoint.username, endpoint.password, *endpoint.params.values(),
    ) if value}
    if endpoint.userinfo or endpoint.query:
        values.add(str(endpoint))
    for _, proxy in connection["proxy_routes"].routes:
        if proxy is not None and proxy.raw_auth is not None:
            values.update(part.decode() for part in proxy.raw_auth if part)
    return tuple(sorted(values, key=len, reverse=True))


async def read_model_catalog(
    connection: Mapping[str, Any], *, deadline: float, owner_key: str | None = None,
) -> tuple[list[LlmDiscoveredModelDTO], bool, LlmProbeErrorDTO | None]:
    protocol = connection["api_mode"]["protocol"]
    endpoint = model_catalog_url(connection)
    if connection.get("auth") is not None:
        raise ValueError("当前 OAuth 服务未提供模型目录合同，请手工添加模型；此结果不代表模型不可调用")
    anthropic = protocol == "anthropic_messages"
    options = connection.get("request_options", {})
    headers = httpx.Headers(options.get("default_headers", {}))
    key = connection.get("api_key", "")
    if anthropic:
        headers.setdefault("anthropic-version", "2023-06-01")
        if key:
            headers["x-api-key"] = key
    elif key:
        headers["Authorization"] = "Bearer " + key
    secret_values = probe_secret_values(connection)

    def fail(category: str, message: str, *, status_code: int | None = None):
        for secret in secret_values:
            message = message.replace(secret, "[redacted]")
        return models, False, LlmProbeErrorDTO(
            category=category, message=message[:4096], status_code=status_code,
        )

    models: list[LlmDiscoveredModelDTO] = []
    total_bytes = 0
    cursor: str | None = None
    routes = connection["proxy_routes"]
    verify = httpx.create_ssl_context(trust_env=True)
    try:
        async with asyncio.timeout_at(deadline), httpx.AsyncClient(
            timeout=None, trust_env=False, follow_redirects=False, verify=verify,
            transport=httpx.AsyncHTTPTransport(verify=verify, trust_env=False),
            mounts=routes.async_mounts(verify=verify),
        ) as client:
            while True:
                page_url = endpoint.copy_merge_params({"after_id": cursor}) if cursor is not None else endpoint
                async with client.stream("GET", page_url, headers=headers) as response:
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > CATALOG_MAX_BYTES:
                            return fail("size_limit", "上游模型目录累计正文超过 2 MiB；仅保留已完整读取的页")
                        chunks.extend(chunk)
                    if response.is_error or response.is_redirect:
                        category = {401: "authentication", 403: "authentication", 404: "path", 429: "rate_limit"}.get(
                            response.status_code, "upstream",
                        )
                        return fail(category, f"上游目录 HTTP {response.status_code}: " + chunks.decode("utf-8", errors="replace")[:4096],
                                    status_code=response.status_code)
                payload = json.loads(chunks)
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    return fail("protocol", "上游模型目录必须返回 data 数组；未推断其它目录路径")
                page = []
                capacity = CATALOG_MAX_MODELS - len(models)
                if len(payload["data"]) > capacity:
                    return fail("count_limit", "上游目录达到 5000 个模型上限；仅保留已完整读取的页")
                for item in payload["data"]:
                    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                        return fail("protocol", "上游模型目录项缺少非空 id；本页不被视为完整结果")
                    name = item.get("display_name", item.get("name"))
                    page.append(LlmDiscoveredModelDTO(model=item["id"], name=name if isinstance(name, str) else None,
                        metadata=discovered_model_metadata(connection["custom_llm_provider"], item["id"],
                            upstream=item, configured=connection["api_mode"].get("model_info", {}), owner_key=owner_key)))
                has_more = payload.get("has_more", False)
                if not isinstance(has_more, bool):
                    return fail("protocol", "上游 has_more 必须是布尔值")
                models.extend(page)
                if len(models) == CATALOG_MAX_MODELS and has_more:
                    return fail("count_limit", "上游目录达到 5000 个模型上限")
                if not has_more:
                    return models, True, None
                next_cursor = payload.get("last_id")
                if not anthropic or not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                    return fail("pagination", "上游目录声明还有数据，但未提供当前协议有效的后续分页标识")
                cursor = next_cursor
    except TimeoutError:
        return fail("timeout", "模型目录发现达到 15 秒绝对期限；仅保留已完整读取的页")
    except httpx.HTTPError as error:
        return fail("network", f"模型目录网络错误: {type(error).__name__}: {error}")
    except (ValueError, UnicodeDecodeError):
        return fail("protocol", "上游模型目录 JSON 或能力字段无效；未修补或替换无效声明")
