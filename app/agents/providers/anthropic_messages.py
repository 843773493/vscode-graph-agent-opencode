"""Anthropic Messages provider 的历史投影和 reasoning 内容适配。"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.agents.provider_api_mode import parse_provider_api_mode
from app.agents.providers.litellm_content import (
    canonicalize_ai_message,
    project_ai_message_content,
)


def _text_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, str) and block:
            blocks.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "output_text"} and isinstance(
            block.get("text"), str
        ):
            blocks.append({"type": "text", "text": block["text"]})
        elif block.get("type") in {
            "image",
            "image_url",
            "input_image",
            "document",
        }:
            blocks.append(dict(block))
    return blocks


class BoxteamAnthropicMessagesModel(ChatAnthropic):
    """使用 LangChain Anthropic 客户端，统一处理本项目的历史 payload。"""

    provider_id: str | None = None
    thinking_blocks_replay: bool = False
    redacted_thinking_replay: bool = False

    def _project_ai_message(self, message: AIMessage) -> AIMessage:
        target_capabilities = {"thinking_blocks"}
        if self.redacted_thinking_replay:
            target_capabilities.add("encrypted_reasoning_replay")
        projection = project_ai_message_content(
            message.content,
            target_provider=self.provider_id,
            target_capabilities=target_capabilities,
            response_metadata=message.response_metadata,
        )
        content = _text_blocks(projection["content"])
        raw_blocks = projection.get("thinking_blocks")
        thinking_blocks: list[dict[str, Any]] = []
        if isinstance(raw_blocks, list):
            for block in raw_blocks:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "thinking" and self.thinking_blocks_replay:
                    thinking_blocks.append(copy.deepcopy(block))
                elif block_type == "redacted_thinking" and self.redacted_thinking_replay:
                    data = block.get("data")
                    if isinstance(data, str) and data:
                        thinking_blocks.append(copy.deepcopy(block))
        content = thinking_blocks + content
        return message.model_copy(update={"content": content})

    def _project_messages(self, messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        return [
            self._project_ai_message(message)
            if isinstance(message, AIMessage)
            else message
            for message in messages
        ]

    def _canonicalize_result(self, result: ChatResult) -> ChatResult:
        generations: list[ChatGeneration] = []
        for generation in result.generations:
            message = generation.message
            if isinstance(message, AIMessage):
                message = canonicalize_ai_message(
                    message,
                    source_provider=self.provider_id,
                )
            generations.append(
                ChatGeneration(
                    message=message,
                    generation_info=generation.generation_info,
                )
            )
        return ChatResult(generations=generations, llm_output=result.llm_output)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = super()._generate(
            self._project_messages(messages),
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )
        return self._canonicalize_result(result)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = await super()._agenerate(
            self._project_messages(messages),
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )
        return self._canonicalize_result(result)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        for chunk in super()._stream(
            self._project_messages(messages),
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        ):
            message = chunk.message
            if not isinstance(message, AIMessageChunk):
                raise TypeError("Anthropic Messages 流必须返回 AIMessageChunk")
            yield chunk

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        async for chunk in super()._astream(
            self._project_messages(messages),
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        ):
            message = chunk.message
            if not isinstance(message, AIMessageChunk):
                raise TypeError("Anthropic Messages 流必须返回 AIMessageChunk")
            yield chunk


def build_anthropic_messages_model(
    *,
    provider: dict[str, Any],
    runtime_config: dict[str, Any],
    request_options: dict[str, Any],
) -> BoxteamAnthropicMessagesModel:
    request_parameters: dict[str, Any] = {}
    for name in ("temperature", "top_p", "max_output_tokens"):
        if name in runtime_config:
            request_parameters[name] = runtime_config[name]
    request_parameters.update(request_options.get("overrides") or {})
    api_mode = parse_provider_api_mode(provider)
    thinking = api_mode.supports_reasoning
    return BoxteamAnthropicMessagesModel(
        model_name=provider["model"],
        api_key=provider.get("api_key"),
        base_url=provider.get("endpoint"),
        default_headers=request_options.get("default_headers") or None,
        streaming=True,
        model_kwargs=request_parameters,
        provider_id=provider.get("id"),
        thinking_blocks_replay=thinking.thinking_blocks_thinking,
        redacted_thinking_replay=thinking.thinking_blocks_redacted_thinking,
    )
