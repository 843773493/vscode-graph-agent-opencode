from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage

from app.agents.provider_capabilities import (
    ProviderCapability,
    detect_required_capabilities_from_messages,
    parse_provider_capabilities,
)


@dataclass(frozen=True)
class ProviderModelCandidate:
    provider_id: str
    model: BaseChatModel
    capabilities: frozenset[ProviderCapability]


class CapabilityRoutingMiddleware(AgentMiddleware[Any, Any, Any]):
    """每次模型请求前根据完整消息上下文选择满足能力要求的模型。"""

    def __init__(self, candidates: Sequence[ProviderModelCandidate]) -> None:
        if not candidates:
            raise ValueError("CapabilityRoutingMiddleware 至少需要一个模型候选")
        self._candidates = tuple(candidates)

    def _matching_candidates(
        self,
        request: ModelRequest[Any],
    ) -> tuple[ProviderModelCandidate, ...]:
        required = detect_required_capabilities_from_messages(request.messages)
        matching = tuple(
            candidate
            for candidate in self._candidates
            if required.issubset(candidate.capabilities)
        )
        if matching:
            return matching

        required_text = ", ".join(sorted(required))
        configured = ", ".join(
            f"{candidate.provider_id}({', '.join(sorted(candidate.capabilities))})"
            for candidate in self._candidates
        )
        raise RuntimeError(
            f"当前模型上下文需要输入能力 [{required_text}]，"
            f"但没有匹配的 provider。已配置 provider: {configured}。"
            "请为支持该输入类型的 provider 配置 capabilities。"
        )

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        last_error: Exception | None = None
        for candidate in self._matching_candidates(request):
            try:
                return handler(request.override(model=candidate.model))
            except Exception as error:
                last_error = error
        if last_error is None:
            raise RuntimeError("模型能力路由没有产生可执行候选")
        raise last_error

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        last_error: Exception | None = None
        for candidate in self._matching_candidates(request):
            try:
                return await handler(request.override(model=candidate.model))
            except Exception as error:
                last_error = error
        if last_error is None:
            raise RuntimeError("模型能力路由没有产生可执行候选")
        raise last_error


def build_provider_model_candidate(
    *,
    provider: dict[str, Any],
    model: BaseChatModel,
) -> ProviderModelCandidate:
    provider_id = str(provider.get("id") or provider.get("model") or "<unknown>")
    return ProviderModelCandidate(
        provider_id=provider_id,
        model=model,
        capabilities=frozenset(parse_provider_capabilities(provider)),
    )
