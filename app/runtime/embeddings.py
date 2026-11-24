from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass

from app.runtime.network_proxy import ProxyRoutes


@dataclass(frozen=True, slots=True)
class LiteLLMEmbeddingComputer:
    """通过已解析的完整模型配置调用远程 Embedding API。"""

    provider_id: str
    model_id: str
    model: str
    endpoint: str
    api_key: str
    custom_llm_provider: str
    default_headers: Mapping[str, str]
    proxy_routes: ProxyRoutes

    @classmethod
    def from_model(
        cls,
        resolved: Mapping[str, object],
        *,
        model: str | None = None,
    ) -> LiteLLMEmbeddingComputer:
        provider_id = _required_string(resolved, "provider_id")
        model_id = _required_string(resolved, "model_id")
        request_options = resolved.get("request_options", {})
        if not isinstance(request_options, Mapping):
            raise TypeError(f"provider {provider_id} 的 request_options 必须是对象")
        default_headers = request_options.get("default_headers", {})
        if not isinstance(default_headers, Mapping):
            raise TypeError(
                f"provider {provider_id} 的 request_options.default_headers 必须是对象"
            )
        normalized_headers: dict[str, str] = {}
        for name, value in default_headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError(
                    f"provider {provider_id} 的 default_headers 键和值必须是字符串"
                )
            normalized_headers[name] = value

        api_key = resolved.get("api_key", "")
        if not isinstance(api_key, str):
            raise TypeError("Embedding Model api_key 必须是字符串")
        if api_key.startswith("secret:") or (api_key.startswith("${") and api_key.endswith("}")):
            raise ValueError("Embedding Model api_key 尚未解析")
        custom_llm_provider = _required_string(resolved, "custom_llm_provider")
        # TODO：其它 Embedding 适配族接通显式无认证前，保留原非空凭据要求，禁止回退进程凭据。
        if custom_llm_provider != "openai" and not api_key:
            raise ValueError(f"Embedding 适配族 {custom_llm_provider} 需要显式非空凭据")
        return cls(
            provider_id=provider_id,
            model_id=model_id,
            model=model or _required_string(resolved, "model"),
            endpoint=_required_string(resolved, "endpoint"),
            api_key=api_key,
            custom_llm_provider=custom_llm_provider,
            default_headers=normalized_headers,
            proxy_routes=resolved["proxy_routes"],
        )

    async def compute(self, inputs: Sequence[str]) -> list[list[float]]:
        input_count = len(inputs)
        if input_count == 0:
            return []

        # LiteLLM 是可选运行时边界，避免仅导入工具定义时加载完整 provider 栈。
        from app.agents.providers.provider_http_client import (
            build_provider_http_handler,
            build_provider_openai_client,
        )
        from app.runtime.litellm_runtime import litellm

        async with AsyncExitStack() as scope:
            # 采用 SDK 实际接收的客户端类型，不能让它忽略错误类型后重建隐式环境客户端。
            if self.custom_llm_provider in {"openai", "together_ai", "nvidia_nim", "litellm_proxy",
                                             "mistral", "fireworks_ai", "nebius", "wandb", "xinference"}:
                # SDK client 持有本次明确凭据，避免 LiteLLM 将空 Key 回退到进程环境。
                client = await scope.enter_async_context(build_provider_openai_client(
                    is_async=True, base_url=self.endpoint, api_key=self.api_key,
                    routes=self.proxy_routes,
                ))
            else:
                # 这些 SDK 分支会忽略传入 client，或在模型 HTTP 前另走全局认证 HTTP。
                if self.custom_llm_provider in {"sagemaker", "gigachat", "watsonx", "github_copilot", "sap"}:
                    raise NotImplementedError(f"Embedding 适配族尚未接入网络策略: {self.custom_llm_provider}")
                provider_config = litellm.utils.ProviderConfigManager.get_provider_embedding_config(
                    model=self.model, provider=litellm.LlmProviders(self.custom_llm_provider),
                )
                if provider_config is None:
                    # TODO: 接入这些专用 SDK 的实际代理注入后移除此限制，禁止绕回隐式环境出口。
                    raise NotImplementedError(f"Embedding 适配族尚未接入网络策略: {self.custom_llm_provider}")
                client = build_provider_http_handler(is_async=True, routes=self.proxy_routes, no_auth=False)
                scope.push_async_callback(client.client.aclose)
            response = await litellm.aembedding(
                model=self.model,
                input=list(inputs),
                api_base=self.endpoint,
                api_key=self.api_key,
                custom_llm_provider=self.custom_llm_provider,
                extra_headers=self.default_headers,
                client=client,
                max_retries=3,
                timeout=60,
            )
        response_items = response.data
        if len(response_items) != input_count:
            raise RuntimeError(
                "Embedding API 返回数量不匹配: "
                f"provider={self.provider_id}, model_id={self.model_id}, model={self.model}, "
                f"expected={input_count}, actual={len(response_items)}"
            )

        vectors_by_input_index: list[list[float] | None] = [None] * input_count
        reported_indexes: list[int] | None = None
        filled_input_count = 0
        for response_position, response_item in enumerate(response_items):
            provider_index = response_item.get("index", response_position)
            if not isinstance(provider_index, int):
                raise TypeError("Embedding API 返回的 index 必须是整数")

            if reported_indexes is not None:
                reported_indexes.append(provider_index)
            elif provider_index != response_position:
                reported_indexes = [*range(response_position), provider_index]

            embedding_value = response_item.get("embedding")
            if not isinstance(embedding_value, list) or not embedding_value:
                raise TypeError("Embedding API 返回了无效的 embedding 向量")
            vector: list[float] = []
            for value in embedding_value:
                if not isinstance(value, (int, float)):
                    raise TypeError("Embedding API 返回的向量必须只包含数值")
                vector.append(float(value))

            if (
                0 <= provider_index < input_count
                and vectors_by_input_index[provider_index] is None
            ):
                vectors_by_input_index[provider_index] = vector
                filled_input_count += 1

        if filled_input_count != input_count:
            if reported_indexes is None:
                reported_indexes = list(range(input_count))
            reported_indexes.sort()
            raise RuntimeError(
                "Embedding API 返回的 index 不连续: "
                f"expected={list(range(input_count))}, actual={reported_indexes}"
            )
        return [vector for vector in vectors_by_input_index if vector is not None]


def _required_string(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Embedding Model 缺少非空字符串字段: {key}")
    return value
