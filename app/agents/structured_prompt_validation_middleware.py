from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.messages import AIMessage

from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.prompting.validation import internal_prompt_metadata, validate_internal_message


class StructuredPromptValidationMiddleware(AgentMiddleware[Any, Any, Any]):
    """在最终模型请求边界验证由项目生成的内部结构消息。"""

    @staticmethod
    def _validate(request: ModelRequest[Any]) -> None:
        for message in request.messages:
            version = message.response_metadata.get("context_source_protocol")
            if version is not None:
                parse_source_blocks(message.content, expected_version=version)
            if internal_prompt_metadata(message.response_metadata) is not None:
                validate_internal_message(message.content, message.response_metadata)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        self._validate(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        self._validate(request)
        return await handler(request)


__all__ = ["StructuredPromptValidationMiddleware"]
