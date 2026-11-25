"""识别模型 run，读取和汇总当前模型调用的 token usage。"""

from __future__ import annotations

from collections.abc import Sequence

from app.schemas.event import ModelTokenUsagePayload

_CHAT_MODEL_RUN_NAMES = {
    "ChatOpenAI",
    "BoxteamLiteLLMChatModel",
    "BoxteamOpenAIResponsesModel",
    "ChatLiteLLM",
    "ChatLiteLLMRouter",
}


def is_chat_model_run_name(name: str) -> bool:
    """按 LangChain 事件的 name 字段识别需要消费的模型 run。"""
    return name in _CHAT_MODEL_RUN_NAMES


def last_model_token_usage(
    usages: Sequence[ModelTokenUsagePayload],
) -> ModelTokenUsagePayload:
    """返回最后一次实际模型请求的 token 统计，不跨请求累计。"""
    return next(
        (
            usage.model_copy(deep=True)
            for usage in reversed(usages)
            if usage.model_calls > 0
        ),
        ModelTokenUsagePayload(),
    )
