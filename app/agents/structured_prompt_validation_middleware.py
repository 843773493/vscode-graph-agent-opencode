from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.messages import AIMessage

from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.records import RuntimeNoticePayload
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
                content = message.content
                if isinstance(content, list):
                    # 复用 canonical 附件合同，不能仅凭 origin/kind 标签跳过附加正文。
                    if (not content or not isinstance(content[0], dict)
                        or content[0].get("type") != "text"):
                        raise ValueError("内部结构消息的多模态正文必须以原 prompt 开始")
                    notice = RuntimeNoticePayload.from_dict({
                        "prompt": content[0].get("text"),
                        "attachments": message.response_metadata.get("attachments"),
                        "attachment_blocks": content[1:],
                    })
                    content = notice.prompt
                validate_internal_message(content, message.response_metadata)

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
