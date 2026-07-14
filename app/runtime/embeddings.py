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
        if not inputs:
            return []

        # LiteLLM 是可选运行时边界，避免仅导入工具定义时加载完整 provider 栈。
        from litellm import aembedding

        response = await aembedding(
            model=self.model,
            input=list(inputs),
            api_base=self.endpoint,
            api_key=self.api_key,
            custom_llm_provider=self.custom_llm_provider,
            extra_headers=dict(self.default_headers),
            max_retries=3,
            timeout=60,
        )
        raw_items = response.data
        if len(raw_items) != len(inputs):
            raise RuntimeError(
                "Embedding API 返回数量不匹配: "
                f"provider={self.provider_id}, model={self.model}, "
                f"expected={len(inputs)}, actual={len(raw_items)}"
            )

        indexed_vectors: list[tuple[int, list[float]]] = []
        for position, item in enumerate(raw_items):
            index = item.get("index", position)
            raw_vector = item.get("embedding")
            if not isinstance(index, int):
                raise TypeError("Embedding API 返回的 index 必须是整数")
            if not isinstance(raw_vector, list) or not raw_vector:
                raise TypeError("Embedding API 返回了无效的 embedding 向量")
            if not all(isinstance(value, (int, float)) for value in raw_vector):
                raise TypeError("Embedding API 返回的向量必须只包含数值")
            indexed_vectors.append((index, [float(value) for value in raw_vector]))

        indexed_vectors.sort(key=lambda item: item[0])
        actual_indexes = [index for index, _vector in indexed_vectors]
        expected_indexes = list(range(len(inputs)))
        if actual_indexes != expected_indexes:
            raise RuntimeError(
                "Embedding API 返回的 index 不连续: "
                f"expected={expected_indexes}, actual={actual_indexes}"
            )
        return [vector for _index, vector in indexed_vectors]


def _required_string(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Embedding provider 缺少非空字符串字段: {key}")
    return value
