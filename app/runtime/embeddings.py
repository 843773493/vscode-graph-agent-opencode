from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LiteLLMEmbeddingComputer:
    """通过已有 LiteLLM provider 配置调用远程 Embedding API。"""

    provider_id: str
    model: str
    endpoint: str
    api_key: str
    custom_llm_provider: str
    default_headers: Mapping[str, str]

    @classmethod
    def from_provider(
        cls,
        provider: Mapping[str, object],
        *,
        model: str | None = None,
    ) -> LiteLLMEmbeddingComputer:
        provider_id = _required_string(provider, "id")
        request_options = provider.get("request_options", {})
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

        resolved_model = model or _required_string(provider, "model")
        return cls(
            provider_id=provider_id,
            model=resolved_model,
            endpoint=_required_string(provider, "endpoint"),
            api_key=_required_string(provider, "api_key"),
            custom_llm_provider=_required_string(provider, "custom_llm_provider"),
            default_headers=normalized_headers,
        )

    async def compute(self, inputs: Sequence[str]) -> list[list[float]]:
        input_count = len(inputs)
        if input_count == 0:
            return []

        # LiteLLM 是可选运行时边界，避免仅导入工具定义时加载完整 provider 栈。
        from app.runtime.litellm_runtime import litellm

        response = await litellm.aembedding(
            model=self.model,
            input=list(inputs),
            api_base=self.endpoint,
            api_key=self.api_key,
            custom_llm_provider=self.custom_llm_provider,
            extra_headers=dict(self.default_headers),
            max_retries=3,
            timeout=60,
        )
        response_items = response.data
        if len(response_items) != input_count:
            raise RuntimeError(
                "Embedding API 返回数量不匹配: "
                f"provider={self.provider_id}, model={self.model}, "
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
        raise ValueError(f"Embedding provider 缺少非空字符串字段: {key}")
    return value
