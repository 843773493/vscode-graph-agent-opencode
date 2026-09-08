"""Provider 响应到项目 LangChain content 的收敛。"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from app.agents.providers.message_content_schema import validate_content_blocks
from app.agents.providers.stream_normalization import stream_content_blocks


def canonicalize_ai_message(
    message: AIMessage,
    *,
    source_provider: str | None = None,
) -> AIMessage:
    """将 provider stream blocks 收敛为可进入 checkpoint 的 LangChain 消息。"""
    del source_provider
    additional_kwargs = dict(message.additional_kwargs or {})
    blocks = stream_content_blocks(message.content)
    for key in ("reasoning_content", "thinking_blocks", "reasoning_items"):
        additional_kwargs.pop(key, None)
    normalized_content = validate_content_blocks(blocks)
    if not blocks and isinstance(message.content, str):
        normalized_content = message.content
    return message.model_copy(
        update={
            "content": normalized_content,
            "additional_kwargs": additional_kwargs,
        }
    )


__all__ = ["canonicalize_ai_message"]
