from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Protocol

from langchain_core.messages import AIMessageChunk


class ModelDeltaSink(Protocol):
    async def accept_message_chunk(self, chunk: AIMessageChunk) -> None: ...


_CURRENT_MODEL_DELTA_SINK: ContextVar[ModelDeltaSink | None] = ContextVar(
    "boxteam_current_model_delta_sink",
    default=None,
)


def set_current_model_delta_sink(sink: ModelDeltaSink) -> Token[ModelDeltaSink | None]:
    return _CURRENT_MODEL_DELTA_SINK.set(sink)


def reset_current_model_delta_sink(token: Token[ModelDeltaSink | None]) -> None:
    _CURRENT_MODEL_DELTA_SINK.reset(token)


def get_current_model_delta_sink() -> ModelDeltaSink | None:
    return _CURRENT_MODEL_DELTA_SINK.get()
