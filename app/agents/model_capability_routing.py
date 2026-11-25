from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextvars import ContextVar
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
from app.core.failures import failure_detail, is_provider_failure
from app.core.turn_execution_scope import ScopeCancelledError
from app.domain.context_compaction import ContextCapacityError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderModelCandidate:
    provider_id: str
    model_id: str
    model: BaseChatModel
    capabilities: frozenset[ProviderCapability]


MODEL_FAILED_CUSTOM_EVENT = "boxteam_model_failed"
_SELECTED_PROVIDER: ContextVar[ProviderModelCandidate | None] = ContextVar("boxteam_selected_provider", default=None)


def current_selected_provider() -> BaseChatModel | None:
    """只在本次路由选择的实际候选内授权投影，不跨请求保留。"""
    candidate = _SELECTED_PROVIDER.get()
    return None if candidate is None else candidate.model


class ProviderCapabilityError(RuntimeError):
    """实际模型请求的必要能力不足；属于本地候选拒绝。"""


def require_provider_capabilities(model: BaseChatModel, messages: Iterable[object]) -> None:
    """只核已投影的实际请求；能力取本次候选或构造时冻结的模型 profile。"""
    candidate = _SELECTED_PROVIDER.get()
    if candidate is not None and candidate.model is model:
        capabilities = candidate.capabilities
    else:
        profile = model.profile or {}
        capabilities = frozenset({"text_input", *(capability for name, capability in (
            ("image_inputs", "image_input"), ("audio_inputs", "audio_input"), ("video_inputs", "video_input"),
        ) if profile.get(name) is True)})
    missing = detect_required_capabilities_from_messages(messages) - capabilities
    if missing:
        raise ProviderCapabilityError(f"provider-capability-insufficient: 当前实际请求缺少模型输入能力 [{', '.join(sorted(missing))}]")


def _response_has_visible_output(
    response: ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any],
) -> bool:
    if isinstance(response, AIMessage):
        if response.tool_calls or response.invalid_tool_calls:
            return True
        return bool(response.text.strip())
    elif isinstance(response, ModelResponse):
        messages = response.result
    elif isinstance(response, ExtendedModelResponse):
        model_response = response.model_response
        if not isinstance(model_response, ModelResponse):
            return False
        messages = model_response.result
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


def _visible_output_error(candidate: ProviderModelCandidate) -> RuntimeError:
    return RuntimeError(
        "模型只返回内部推理，没有用户可见正文或工具调用。"
        f" provider_id={candidate.provider_id} model={candidate.model_id}"
    )


def _failure_payload(
    candidate: ProviderModelCandidate,
    error: Exception,
    *,
    error_message: str,
) -> dict[str, str]:
    return {
        "provider_id": candidate.provider_id,
        "model": candidate.model_id,
        "error_type": type(error).__name__,
        "error": error_message,
    }


class CapabilityRoutingMiddleware(AgentMiddleware[Any, Any, Any]):
    """按配置顺序准备候选，由实际投影核验容量和能力后才发送。"""

    def __init__(self, candidates: Sequence[ProviderModelCandidate]) -> None:
        if not candidates:
            raise ValueError("CapabilityRoutingMiddleware 至少需要一个模型候选")
        self._candidates = tuple(candidates)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        last_error: Exception | None = None
        candidates = self._candidates
        for index, candidate in enumerate(candidates):
            token = _SELECTED_PROVIDER.set(candidate)
            try:
                response = handler(request.override(model=candidate.model))
            except (ContextCapacityError, ProviderCapabilityError) as error:
                last_error = error
                logger.info("模型候选准备拒绝: provider_id=%s model=%s: %s",
                            candidate.provider_id, candidate.model_id, error)
                continue
            except ScopeCancelledError:
                # 父 turn 的用户中断、Job 总超时和内部执行取消都已经是
                # 明确的执行边界，不能被 fallback 当成 provider 失败再次发起请求。
                # 只有适配器标记的上游请求/流迭代异常才能触发 fallback。
                raise
            except Exception as error:
                if not is_provider_failure(error):
                    raise
                last_error = error
                dispatch_custom_event(
                    MODEL_FAILED_CUSTOM_EVENT,
                    _failure_payload(candidate, error, error_message=failure_detail(
                        error, stage="model-provider",
                        label=f"模型候选调用失败: provider_id={candidate.provider_id}, model={candidate.model_id}",
                    )),
                )
                continue
            finally:
                _SELECTED_PROVIDER.reset(token)

            if (
                index == len(candidates) - 1
                or _response_has_visible_output(response)
            ):
                return response
            error = _visible_output_error(candidate)
            last_error = error
            logger.warning(
                "模型候选只返回内部推理，准备尝试 fallback: provider_id=%s model=%s",
                candidate.provider_id,
                candidate.model_id,
            )
            dispatch_custom_event(
                MODEL_FAILED_CUSTOM_EVENT,
                _failure_payload(candidate, error, error_message=str(error)),
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
        candidates = self._candidates
        for index, candidate in enumerate(candidates):
            token = _SELECTED_PROVIDER.set(candidate)
            try:
                response = await handler(request.override(model=candidate.model))
            except (ContextCapacityError, ProviderCapabilityError) as error:
                last_error = error
                logger.info("模型候选准备拒绝: provider_id=%s model=%s: %s",
                            candidate.provider_id, candidate.model_id, error)
                continue
            except ScopeCancelledError:
                # 取消不是 provider 能力降级；保留原始 reason 交给 Job/stream
                # 状态机分类，避免 backup_4 覆盖用户选择或把 Job 继续拖成 running。
                raise
            except Exception as error:
                if not is_provider_failure(error):
                    raise
                last_error = error
                await adispatch_custom_event(
                    MODEL_FAILED_CUSTOM_EVENT,
                    _failure_payload(candidate, error, error_message=failure_detail(
                        error, stage="model-provider",
                        label=f"模型候选调用失败: provider_id={candidate.provider_id}, model={candidate.model_id}",
                    )),
                )
                continue
            finally:
                _SELECTED_PROVIDER.reset(token)

            if (
                index == len(candidates) - 1
                or _response_has_visible_output(response)
            ):
                return response
            error = _visible_output_error(candidate)
            last_error = error
            logger.warning(
                "模型候选只返回内部推理，准备尝试 fallback: provider_id=%s model=%s",
                candidate.provider_id,
                candidate.model_id,
            )
            await adispatch_custom_event(
                MODEL_FAILED_CUSTOM_EVENT,
                _failure_payload(candidate, error, error_message=str(error)),
            )
        if last_error is None:
            raise RuntimeError("模型能力路由没有产生可执行候选")
        raise last_error


def build_provider_model_candidate(
    *,
    provider: dict[str, Any],
    model: BaseChatModel,
) -> ProviderModelCandidate:
    return ProviderModelCandidate(
        provider_id=provider["provider_id"],
        model_id=provider["model_id"],
        model=model,
        capabilities=frozenset(parse_provider_capabilities(provider)),
    )
