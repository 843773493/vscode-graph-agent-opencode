from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.callbacks.manager import (
    adispatch_custom_event,
    dispatch_custom_event,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage

from app.agents.provider_capabilities import (
    ProviderCapability,
    detect_required_capabilities_from_messages,
    parse_provider_capabilities,
)
from app.core.turn_execution_scope import ScopeCancelledError


@dataclass(frozen=True)
class ProviderModelCandidate:
    provider_id: str
    model_id: str
    model: BaseChatModel
    capabilities: frozenset[ProviderCapability]


MODEL_FAILED_CUSTOM_EVENT = "boxteam_model_failed"


def _response_has_visible_output(
    response: ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any],
) -> bool:
    if isinstance(response, AIMessage):
        messages = [response]
    elif isinstance(response, ModelResponse):
        messages = list(response.result)
    elif isinstance(response, ExtendedModelResponse):
        model_response = response.model_response
        if not isinstance(model_response, ModelResponse):
            return False
        messages = list(model_response.result)
    else:
        return False

    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        if message.tool_calls or message.invalid_tool_calls:
            return True
        if message.text.strip():
            return True
    return False


def _validate_visible_response(
    candidate: ProviderModelCandidate,
    response: ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any],
) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
    if not _response_has_visible_output(response):
        raise RuntimeError(
            "模型只返回内部推理，没有用户可见正文或工具调用。"
            f" provider_id={candidate.provider_id} model={candidate.model_id}"
        )
    return response


def _failure_payload(
    candidate: ProviderModelCandidate,
    error: Exception,
) -> dict[str, str]:
    return {
        "provider_id": candidate.provider_id,
        "model": candidate.model_id,
        "error_type": type(error).__name__,
        "error": str(error),
    }


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
            "请在 provider.api_mode.model_info 中配置对应的 supports_* 能力。"
        )

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        last_error: Exception | None = None
        for candidate in self._matching_candidates(request):
            try:
                response = handler(request.override(model=candidate.model))
                return _validate_visible_response(candidate, response)
            except ScopeCancelledError:
                # 父 turn 的用户中断、Job 总超时和内部执行取消都已经是
                # 明确的执行边界，不能被 fallback 当成 provider 失败再次发起请求。
                # 候选模型自己的可重试失败仍通过普通 Exception 走 fallback。
                raise
            except Exception as error:  # noqa: BLE001 - provider 失败时必须尝试后续候选
                last_error = error
                dispatch_custom_event(
                    MODEL_FAILED_CUSTOM_EVENT,
                    _failure_payload(candidate, error),
                )
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
                response = await handler(request.override(model=candidate.model))
                return _validate_visible_response(candidate, response)
            except ScopeCancelledError:
                # 取消不是 provider 能力降级；保留原始 reason 交给 Job/stream
                # 状态机分类，避免 backup_4 覆盖用户选择或把 Job 继续拖成 running。
                raise
            except Exception as error:  # noqa: BLE001 - provider 失败时必须尝试后续候选
                last_error = error
                await adispatch_custom_event(
                    MODEL_FAILED_CUSTOM_EVENT,
                    _failure_payload(candidate, error),
                )
        if last_error is None:
            raise RuntimeError("模型能力路由没有产生可执行候选")
        raise last_error


def build_provider_model_candidate(
    *,
    provider: dict[str, Any],
    model: BaseChatModel,
) -> ProviderModelCandidate:
    provider_id = str(provider.get("id") or provider.get("model") or "<unknown>")
    model_id = str(provider.get("model") or provider_id)
    return ProviderModelCandidate(
        provider_id=provider_id,
        model_id=model_id,
        model=model,
        capabilities=frozenset(parse_provider_capabilities(provider)),
    )
