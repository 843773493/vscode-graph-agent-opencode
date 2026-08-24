from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, ClassVar

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

from app.agents.provider_api_mode import parse_provider_api_mode
from app.agents.providers.litellm_chat import (
    BoxteamLiteLLMChatModel,
    _message_chunk_token,
    _StreamPartState,
)
from app.agents.providers.litellm_content import (
    project_ai_message_content,
)
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

    responses_include: ClassVar[list[str]] = ["reasoning.encrypted_content"]
    responses_store: bool = False
    reasoning_items_summary_replay: bool = False
    reasoning_items_encrypted_replay: bool = False

    def _history_messages(self, messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        prepared: list[BaseMessage] = []
        for message in messages:
            if not isinstance(message, AIMessage):
                prepared.append(message)
                continue

            projection = project_ai_message_content(
                message.content,
                target_provider=self.provider_id,
                target_capabilities={
                    "reasoning_items",
                    *(
                        {"reasoning_summary"}
                        if self.reasoning_items_summary_replay
                        else set()
                    ),
                    *(
                        {"encrypted_reasoning_replay"}
                        if self.reasoning_items_encrypted_replay
                        else set()
                    ),
                },
                response_metadata=message.response_metadata,
            )
            content = projection["content"]
            projected_items = projection.get("reasoning_items")
            prepared_content: list[Any] = []
            if isinstance(content, str) and content:
                prepared_content.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type in {"text", "output_text"}:
                        text = block.get("text")
                        if isinstance(text, str):
                            prepared_content.append({"type": "text", "text": text})
                    elif block_type == "refusal":
                        prepared_content.append(dict(block))
                    elif block_type in {"image", "image_url", "input_image"}:
                        prepared_content.append(_without_server_state(block))
            if isinstance(projected_items, list):
                prepared_content.extend(
                    dict(item)
                    for item in projected_items
                    if isinstance(item, dict)
                )
            has_tool_calls = bool(
                message.tool_calls
                or message.additional_kwargs.get("tool_calls")
                or message.additional_kwargs.get("function_call")
            )
            reasoning_content = [
                block
                for block in prepared_content
                if isinstance(block, dict) and block.get("type") == "reasoning"
            ]
            visible_content = [
                block
                for block in prepared_content
                if not (isinstance(block, dict) and block.get("type") == "reasoning")
            ]
            if reasoning_content and (visible_content or has_tool_calls):
                prepared.append(
                    message.model_copy(
                        update={
                            "content": reasoning_content,
                            "tool_calls": [],
                        }
                    )
                )
                prepared_content = visible_content
            if prepared_content or has_tool_calls:
                prepared.append(message.model_copy(update={"content": prepared_content}))
        return prepared

    @staticmethod
    def _normalize_response_block(block: dict[str, Any]) -> dict[str, Any] | None:
        block_type = block.get("type")
        if block_type in {"reasoning", "text", "output_text", "refusal"}:
            # Responses/LiteLLM 已经完成了 provider block 的组装。这里仅做
            # 类型归一，不按字段白名单重建，保留 annotations、phase 以及
            # provider 后续增加的字段。
            result = copy.deepcopy(block)
            if block_type == "output_text":
                result["type"] = "text"
            return result
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
        return ChatGenerationChunk(
            message=message.model_copy(update={"content": content}),
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
        responses_payload = _construct_responses_api_payload(
            self._history_messages(messages),
            payload,
        )
        # TODO: LiteLLM 的 chatgpt provider 原生把 system/developer input
        # 合并进 instructions 后，删除此兼容转换。
        if self.custom_llm_provider == "chatgpt":
            return self._move_system_messages_to_instructions(responses_payload)
        return responses_payload

    @staticmethod
    def _move_system_messages_to_instructions(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """ChatGPT Codex endpoint 只接受 instructions，不接受 system input。"""
        input_items = payload.get("input")
        if not isinstance(input_items, list):
            return payload

        instruction_parts: list[str] = []
        remaining_items: list[Any] = []
        for item in input_items:
            if not isinstance(item, dict) or item.get("role") not in {
                "system",
                "developer",
            }:
                remaining_items.append(item)
                continue

            content = item.get("content")
            if isinstance(content, str):
                instruction_parts.append(content)
                continue
            if not isinstance(content, list):
                raise TypeError(
                    "ChatGPT system message content 必须是字符串或内容块列表"
                )
            for block in content:
                if not isinstance(block, dict) or block.get("type") not in {
                    "text",
                    "input_text",
                }:
                    raise TypeError("ChatGPT system message 只支持文本内容块")
                text = block.get("text")
                if not isinstance(text, str):
                    raise TypeError("ChatGPT system message 文本块缺少 text 字符串")
                instruction_parts.append(text)

        if not instruction_parts:
            return payload
        existing_instructions = payload.get("instructions")
        if existing_instructions is not None:
            if not isinstance(existing_instructions, str):
                raise TypeError("Responses instructions 必须是字符串")
            instruction_parts.insert(0, existing_instructions)

        return {
            **payload,
            "input": remaining_items,
            "instructions": "\n\n".join(instruction_parts),
        }

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
            output_index = int(event.output_index)
            if current_output_index != output_index:
                current_index += 1
            current_output_index = output_index
            current_sub_index = 0
            block = self._normalize_response_block(event_item_dict)
            if block is None:
                raise RuntimeError("Responses reasoning item 转换失败")
            has_body = bool(
                block.get("content")
                or block.get("summary")
                or block.get("encrypted_content")
            )
            if not has_body:
                return current_index, current_output_index, current_sub_index, None
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
    api_mode = parse_provider_api_mode(provider)
    if api_mode.protocol != "responses":
        raise ValueError(
            "build_openai_responses_model 只接受 api_mode.protocol='responses'"
        )
    request_parameters: dict[str, Any] = {}
    for name in ("temperature", "top_p", "max_output_tokens"):
        if name in runtime_config:
            request_parameters[name] = runtime_config[name]
    request_parameters.update(request_options.get("overrides") or {})
    if provider["custom_llm_provider"] == "chatgpt":
        if prompt_cache_key is not None:
            request_parameters["litellm_session_id"] = prompt_cache_key
    elif api_mode.request_features.prompt_cache_key:
        request_parameters["prompt_cache_key"] = (
            prompt_cache_key or f"boxteam:{provider['id']}"
        )

    responses_include = (
        ["reasoning.encrypted_content"]
        if api_mode.supports_reasoning.reasoning_items_encrypted_content
        else []
    )

    return BoxteamOpenAIResponsesModel(
        model=provider["model"],
        api_key=provider.get("api_key"),
        api_base=provider.get("endpoint"),
        extra_headers=request_options.get("default_headers") or None,
        custom_llm_provider=provider["custom_llm_provider"],
        max_retries=3,
        streaming=True,
        model_kwargs=request_parameters,
        provider_id=provider.get("id"),
        responses_include=responses_include,
        reasoning_items_summary_replay=api_mode.supports_reasoning.reasoning_items_summary,
        reasoning_items_encrypted_replay=api_mode.supports_reasoning.reasoning_items_encrypted_content,
    )
