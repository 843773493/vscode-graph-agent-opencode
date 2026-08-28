"""Anthropic Messages provider 的历史投影和 reasoning 内容适配。"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import anthropic
from langchain_anthropic import ChatAnthropic

# TODO: langchain-anthropic 暂未公开 raw stream 转换所需的 helper；升级到公开 API 后移除这些私有导入。
from langchain_anthropic.chat_models import (
    _compact_in_params,
    _documents_in_params,
    _handle_anthropic_bad_request,
    _thinking_in_params,
    _tools_in_params,
)
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
from app.core.cancelable_stream import CancelableStream
from app.core.model_delta_context import get_current_model_delta_sink
from app.core.turn_execution_scope import get_current_turn_execution_scope


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
            if self._message_chunk_has_semantic_delta(message) and get_current_model_delta_sink() is not None:
                raise RuntimeError(
                    "同步 Anthropic 模型流不能承载异步消息流 delta hook；"
                    "AgentLoop 必须使用异步模型流"
                )
            yield chunk

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        *,
        stream_usage: bool | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        if stream_usage is None:
            stream_usage = self.stream_usage
        if stream_usage is None:
            stream_usage = True
        kwargs["stream"] = True
        payload = self._get_request_payload(
            self._project_messages(messages),
            stop=stop,
            **kwargs,
        )
        try:
            stream = await self._acreate(payload)
            coerce_content_to_string = (
                not _tools_in_params(payload)
                and not _documents_in_params(payload)
                and not _thinking_in_params(payload)
                and not _compact_in_params(payload)
            )
            block_start_event = None
            scope = get_current_turn_execution_scope()
            signal = scope.effective_cancellation_signal if scope else None
            async with CancelableStream(stream, signal) as cancelable_stream:
                async for event in cancelable_stream:
                    if scope is not None:
                        scope.raise_if_cancelled()
                    msg, block_start_event = self._make_message_chunk_from_anthropic_event(
                        event,
                        stream_usage=stream_usage,
                        coerce_content_to_string=coerce_content_to_string,
                        block_start_event=block_start_event,
                    )
                    if msg is None:
                        continue
                    chunk = ChatGenerationChunk(message=msg)
                    delta_sink = get_current_model_delta_sink()
                    if (
                        self._message_chunk_has_semantic_delta(msg)
                        and delta_sink is not None
                    ):
                        await delta_sink.accept_message_chunk(msg)
                    if run_manager and isinstance(msg.content, str):
                        await run_manager.on_llm_new_token(msg.content, chunk=chunk)
                    yield chunk
        except anthropic.BadRequestError as error:
            _handle_anthropic_bad_request(error)

    @staticmethod
    def _message_chunk_has_semantic_delta(message: AIMessageChunk) -> bool:
        content = getattr(message, "content", None)
        if isinstance(content, str) and content:
            return True
        if isinstance(content, list) and any(content):
            return True
        return bool(getattr(message, "tool_call_chunks", None))


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
