from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import litellm
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.ai import InputTokenDetails, UsageMetadata
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai.chat_models.base import (
    _construct_responses_api_payload,
    _convert_responses_chunk_to_generation_chunk,
)

from app.agents.providers.litellm_chat import (
    BoxteamLiteLLMChatModel,
    _StreamPartState,
    _message_chunk_token,
)
from app.services.mapping.agent_content_mapper import extract_reasoning_summary
from app.agents.upstream_request_trace import attach_upstream_trace_callback


def _without_server_state(item: dict[str, Any]) -> dict[str, Any]:
    """store=false 时仅回放可移植的 Response item 内容。"""
    result = dict(item)
    result.pop("id", None)
    result.pop("status", None)
    result.pop("index", None)
    return result


def _responses_usage_metadata(usage: Any) -> UsageMetadata:
    raw = (
        dict(usage)
        if isinstance(usage, dict)
        else usage.model_dump(exclude_none=True)
        if hasattr(usage, "model_dump")
        else {}
    )
    input_tokens = int(raw.get("input_tokens") or 0)
    output_tokens = int(raw.get("output_tokens") or 0)
    metadata: UsageMetadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(raw.get("total_tokens") or input_tokens + output_tokens),
    }
    details = raw.get("input_tokens_details") or {}
    input_details: InputTokenDetails = {}
    if details.get("cached_tokens") is not None:
        input_details["cache_read"] = int(details["cached_tokens"])
    if details.get("cache_write_tokens") is not None:
        input_details["cache_creation"] = int(details["cache_write_tokens"])
    if input_details:
        metadata["input_token_details"] = input_details
    return metadata


class BoxteamOpenAIResponsesModel(BoxteamLiteLLMChatModel):
    """LiteLLM Responses API 包装层，保留加密 reasoning 和标准 content blocks。"""

    responses_include: list[str] = ["reasoning.encrypted_content"]
    responses_store: bool = False

    @staticmethod
    def _history_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        prepared: list[BaseMessage] = []
        for message in messages:
            if not isinstance(message, AIMessage) or not isinstance(message.content, list):
                prepared.append(message)
                continue

            content: list[Any] = []
            for block in message.content:
                if not isinstance(block, dict):
                    content.append(block)
                    continue
                block_type = block.get("type")
                extras = block.get("extras")
                if block_type == "reasoning" and isinstance(extras, dict):
                    response_item = extras.get("response_item")
                    if isinstance(response_item, dict):
                        content.append(_without_server_state(response_item))
                        continue
                if block_type in {"text", "output_text"}:
                    text_block: dict[str, Any] = {
                        "type": "text",
                        "text": block.get("text", ""),
                    }
                    if isinstance(extras, dict) and isinstance(extras.get("phase"), str):
                        text_block["phase"] = extras["phase"]
                    content.append(text_block)
                    continue
                content.append(_without_server_state(block))
            prepared.append(message.model_copy(update={"content": content}))
        return prepared

    @staticmethod
    def _normalize_response_block(block: dict[str, Any]) -> dict[str, Any] | None:
        block_type = block.get("type")
        if block_type == "reasoning":
            reasoning = extract_reasoning_summary(block.get("summary"))
            result: dict[str, Any] = {
                "type": "reasoning",
                "reasoning": reasoning,
                **({"id": block["id"]} if isinstance(block.get("id"), str) else {}),
            }
            if isinstance(block.get("encrypted_content"), str) and block[
                "encrypted_content"
            ]:
                result["extras"] = {
                    "response_item": _without_server_state(block)
                }
            return result
        if block_type in {"text", "output_text"}:
            extras: dict[str, Any] = {}
            if isinstance(block.get("phase"), str):
                extras["phase"] = block["phase"]
            result: dict[str, Any] = {
                "type": "text",
                "text": block.get("text", ""),
            }
            if isinstance(block.get("id"), str):
                result["id"] = block["id"]
            if extras:
                result["extras"] = extras
            return result
        if block_type == "refusal":
            return dict(block)
        return None

    def _normalize_generation_chunk(
        self,
        generation_chunk: ChatGenerationChunk,
        part_state: _StreamPartState,
    ) -> ChatGenerationChunk:
        message = generation_chunk.message
        content: Any = message.content
        if isinstance(content, list):
            normalized = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                normalized_block = self._normalize_response_block(block)
                if normalized_block is not None:
                    normalized.append(part_state.decorate(normalized_block))
            content = normalized
        metadata = dict(message.response_metadata)
        metadata.setdefault("provider_id", self.provider_id)
        return ChatGenerationChunk(
            message=message.model_copy(
                update={"content": content, "response_metadata": metadata}
            ),
            generation_info=generation_chunk.generation_info,
        )

    def _responses_payload(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        if stop is not None:
            raise ValueError("Responses API 不支持 stop 参数")
        payload = {
            key: value
            for key, value in self._client_params.items()
            if value is not None
        }
        payload.update(kwargs)
        payload.update(
            {
                "stream": True,
                "include": list(self.responses_include),
                "store": self.responses_store,
            }
        )
        return _construct_responses_api_payload(
            self._history_messages(messages),
            payload,
        )

    def _convert_response_event(
        self,
        event: Any,
        *,
        current_index: int,
        current_output_index: int,
        current_sub_index: int,
        part_state: _StreamPartState,
        original_schema: Any,
    ) -> tuple[int, int, int, ChatGenerationChunk | None]:
        event_type = getattr(event, "type", None)
        event_item = getattr(event, "item", None)
        event_item_dict = (
            dict(event_item)
            if isinstance(event_item, dict)
            else event_item.model_dump(exclude_none=True, mode="json")
            if hasattr(event_item, "model_dump")
            else {}
        )
        if (
            event_type == "response.output_item.added"
            and event_item_dict.get("type") == "reasoning"
        ):
            output_index = int(getattr(event, "output_index"))
            if current_output_index != output_index:
                current_index += 1
            current_output_index = output_index
            current_sub_index = 0
            block: dict[str, Any] = {
                "type": "reasoning",
                "reasoning": extract_reasoning_summary(event_item_dict.get("summary")),
            }
            if isinstance(event_item_dict.get("id"), str):
                block["id"] = event_item_dict["id"]
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(content=[part_state.decorate(block)])
            )
            return current_index, current_output_index, current_sub_index, chunk
        if (
            event_type == "response.output_item.done"
            and event_item_dict.get("type") == "reasoning"
        ):
            block = self._normalize_response_block(event_item_dict)
            if block is None:
                raise RuntimeError("Responses reasoning item 转换失败")
            block["reasoning"] = ""
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(content=[part_state.decorate(block)])
            )
            return current_index, current_output_index, current_sub_index, chunk
        if event_type in {"response.completed", "response.incomplete"}:
            response = getattr(event, "response", None)
            usage = getattr(response, "usage", None)
            metadata = {
                "model_provider": "litellm",
                "custom_llm_provider": self.custom_llm_provider,
                "provider_id": self.provider_id,
            }
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    usage_metadata=(
                        _responses_usage_metadata(usage) if usage is not None else None
                    ),
                    response_metadata=metadata,
                    chunk_position="last",
                )
            )
            return current_index, current_output_index, current_sub_index, chunk
        if event_type in {"response.failed", "error"}:
            raise RuntimeError(f"LiteLLM Responses 请求失败: {event!r}")

        # TODO: langchain-openai 暴露 Responses 事件转换公共 API 后移除私有 helper。
        (
            current_index,
            current_output_index,
            current_sub_index,
            generation_chunk,
        ) = _convert_responses_chunk_to_generation_chunk(
            event,
            current_index,
            current_output_index,
            current_sub_index,
            schema=original_schema,
            output_version="responses/v1",
            has_reasoning=False,
        )
        if generation_chunk is not None:
            generation_chunk = self._normalize_generation_chunk(
                generation_chunk,
                part_state,
            )
        return (
            current_index,
            current_output_index,
            current_sub_index,
            generation_chunk,
        )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        raw_payload = self._responses_payload(messages, stop, kwargs)
        payload = attach_upstream_trace_callback(
            raw_payload,
            fallback_request=raw_payload,
        )
        stream = litellm.responses(**payload)
        current_index = current_output_index = current_sub_index = -1
        part_state = _StreamPartState()
        original_schema = kwargs.get("response_format")
        for event in stream:
            (
                current_index,
                current_output_index,
                current_sub_index,
                generation_chunk,
            ) = self._convert_response_event(
                event,
                current_index=current_index,
                current_output_index=current_output_index,
                current_sub_index=current_sub_index,
                part_state=part_state,
                original_schema=original_schema,
            )
            if generation_chunk is None:
                continue
            if run_manager:
                run_manager.on_llm_new_token(
                    _message_chunk_token(generation_chunk.message),
                    chunk=generation_chunk,
                )
            yield generation_chunk

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """通过 LiteLLM 流式调用 Responses，并补上 done reasoning 密文。"""
        raw_payload = self._responses_payload(messages, stop, kwargs)
        payload = attach_upstream_trace_callback(
            raw_payload,
            fallback_request=raw_payload,
        )
        stream = await litellm.aresponses(**payload)
        original_schema = kwargs.get("response_format")
        current_index = current_output_index = current_sub_index = -1
        part_state = _StreamPartState()
        async for event in stream:
            (
                current_index,
                current_output_index,
                current_sub_index,
                generation_chunk,
            ) = self._convert_response_event(
                event,
                current_index=current_index,
                current_output_index=current_output_index,
                current_sub_index=current_sub_index,
                part_state=part_state,
                original_schema=original_schema,
            )
            if generation_chunk is None:
                continue
            if run_manager:
                await run_manager.on_llm_new_token(
                    _message_chunk_token(generation_chunk.message),
                    chunk=generation_chunk,
                )
            yield generation_chunk


def build_openai_responses_model(
    *,
    provider: dict[str, Any],
    runtime_config: dict[str, Any],
    request_options: dict[str, Any],
    prompt_cache_key: str | None,
) -> BoxteamOpenAIResponsesModel:
    request_parameters: dict[str, Any] = {}
    for name in ("temperature", "top_p", "max_output_tokens"):
        if name in runtime_config:
            request_parameters[name] = runtime_config[name]
    request_parameters.update(request_options.get("overrides") or {})
    request_parameters.update(
        {
            "prompt_cache_key": prompt_cache_key or f"boxteam:{provider['id']}",
        }
    )

    return BoxteamOpenAIResponsesModel(
        model=provider["model"],
        api_key=provider["api_key"],
        api_base=provider.get("endpoint"),
        extra_headers=request_options.get("default_headers") or None,
        custom_llm_provider=provider["custom_llm_provider"],
        max_retries=3,
        streaming=True,
        model_kwargs=request_parameters,
        provider_id=provider.get("id"),
    )
