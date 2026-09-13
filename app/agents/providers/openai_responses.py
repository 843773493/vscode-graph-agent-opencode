from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any, ClassVar

import litellm
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
)
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai.chat_models.base import (
    _construct_responses_api_payload,
    _convert_responses_chunk_to_generation_chunk,
)

from app.agents.provider_api_mode import parse_provider_api_mode
from app.agents.provider_capabilities import parse_provider_capabilities
from app.agents.providers.litellm_chat import (
    BoxteamLiteLLMChatModel,
    _message_chunk_token,
    _StreamPartState,
)
from app.agents.providers.openai_response_history import (
    DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS,
    ResponsesPayloadBuildTimeoutError,
    ResponsesStreamOpenTimeoutError,
    _project_tool_history,
    _reasoning_summary_content,
    _responses_usage_metadata,
    _without_server_state,
)
from app.agents.request_replay_middleware import read_native_request_projection
from app.agents.upstream_request_trace import (
    attach_upstream_trace_callback,
    record_upstream_response,
)
from app.core.cancelable_stream import CancelableStream, close_async_stream
from app.core.model_delta_context import get_current_model_delta_sink
from app.core.turn_execution_scope import get_current_turn_execution_scope
from app.services.infrastructure.rollout_context.provider.native_request import (
    bind_native_request_payload,
)
from app.services.mapping.agent_content_mapper import extract_reasoning_summary
from app.services.mapping.itemized.provider_request import (
    project_ai_message_content,
    project_user_message_content,
)

logger = logging.getLogger(__name__)

# Responses provider 建立流的等待必须独立于 Agent 事件流 watchdog。
# 否则上游连接在没有产生任何 LangChain 事件时会一直占住 AgentLoop，
# 最终被外层错误归类为首事件超时，既没有可诊断的 provider 阶段，也无法触发
# 已配置的候选模型 fallback。
DEFAULT_RESPONSES_STREAM_OPEN_TIMEOUT_SECONDS = 45.0


class BoxteamOpenAIResponsesModel(BoxteamLiteLLMChatModel):
    """LiteLLM Responses API 包装层，保留加密 reasoning 和标准 content blocks。"""

    responses_include: ClassVar[list[str]] = ["reasoning.encrypted_content"]
    responses_store: bool = False
    reasoning_items_summary_replay: bool = False
    reasoning_items_encrypted_replay: bool = False
    image_input_replay: bool = False

    def _history_messages(
        self,
        messages: Sequence[BaseMessage],
        *,
        deadline: float | None = None,
    ) -> list[BaseMessage]:
        normalized_messages = _project_tool_history(messages, deadline=deadline)
        prepared: list[BaseMessage] = []
        for message in normalized_messages:
            if deadline is not None and time.monotonic() >= deadline:
                raise ResponsesPayloadBuildTimeoutError(
                    "Responses 历史消息 payload 投影超过时间预算: "
                    f"timeout_seconds={DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS:g}"
                )
            if not isinstance(message, AIMessage):
                if isinstance(message, HumanMessage):
                    projection = project_user_message_content(
                        message.content,
                        target_format="responses",
                        image_input=self.image_input_replay,
                    )
                    prepared.append(
                        message.model_copy(update={"content": projection["content"]})
                    )
                else:
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
                    if block_type in {"text", "input_text", "output_text"}:
                        text = block.get("text")
                        if isinstance(text, str):
                            prepared_content.append({"type": "text", "text": text})
                    elif block_type == "refusal":
                        prepared_content.append(dict(block))
                    elif block_type in {"image", "image_url", "input_image"}:
                        prepared_content.append(_without_server_state(block))
            if isinstance(projected_items, list):
                prepared_content.extend(
                    dict(item) for item in projected_items if isinstance(item, dict)
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
                prepared.append(
                    message.model_copy(update={"content": prepared_content})
                )
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
        *,
        deadline: float | None = None,
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
        native = read_native_request_projection()
        responses_payload = (
            bind_native_request_payload(native, payload)
            if native is not None
            else _construct_responses_api_payload(
                self._history_messages(messages, deadline=deadline), payload
            )
        )
        if deadline is not None and time.monotonic() >= deadline:
            raise ResponsesPayloadBuildTimeoutError(
                "Responses 请求 payload 构造超过时间预算: "
                f"timeout_seconds={DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS:g}"
            )
        responses_payload = self._strip_reasoning_content_from_payload(
            responses_payload
        )
        # TODO: LiteLLM 的 chatgpt provider 原生把 system/developer input
        # 合并进 instructions 后，删除此兼容转换。
        if self.custom_llm_provider == "chatgpt":
            return self._move_system_messages_to_instructions(responses_payload)
        return responses_payload

    @staticmethod
    def _strip_reasoning_content_from_payload(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """在最终请求边界移除 Responses reasoning 的输出态 content。"""
        input_items = payload.get("input")
        if not isinstance(input_items, list):
            return payload
        for item in input_items:
            if isinstance(item, dict) and item.get("type") == "reasoning":
                # TODO: 等所有兼容 Responses provider 支持 reasoning.content
                # 的历史输入后，再删除这个最终边界清洗。
                item.pop("content", None)
        return payload

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
            provider_item_id = event_item_dict.get("id")
            if isinstance(provider_item_id, str) and event_item_dict.get("summary"):
                part_state.reasoning_summary_provider_ids.add(provider_item_id)
            output_index = int(event.output_index)
            if current_output_index != output_index:
                current_index += 1
            current_output_index = output_index
            current_sub_index = 0
            block = self._normalize_response_block(
                {
                    key: value
                    for key, value in event_item_dict.items()
                    if key not in {"status", "encrypted_content"}
                }
            )
            if block is None:
                raise RuntimeError("Responses reasoning item 转换失败")
            block = _reasoning_summary_content(block)
            has_body = bool(
                block.get("content")
                or extract_reasoning_summary(block.get("summary"))
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
            provider_item_id = event_item_dict.get("id")
            has_summary = (
                isinstance(provider_item_id, str)
                and provider_item_id in part_state.reasoning_summary_provider_ids
            )
            block = self._normalize_response_block(
                {
                    key: value
                    for key, value in event_item_dict.items()
                    if key != "summary" or not has_summary
                }
            )
            if block is None:
                raise RuntimeError("Responses reasoning item 转换失败")
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(content=[part_state.decorate(block)])
            )
            return current_index, current_output_index, current_sub_index, chunk
        if event_type in {"response.completed", "response.incomplete"}:
            response = getattr(event, "response", None)
            record_upstream_response(response)
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

        if (
            event_type == "response.output_item.added"
            and event_item_dict.get("type") == "function_call"
        ):
            output_index = getattr(event, "output_index", None)
            if isinstance(output_index, bool) or not isinstance(output_index, int):
                raise RuntimeError("Responses function_call 缺少有效 output_index")
            item_id = event_item_dict.get("id")
            call_id = event_item_dict.get("call_id")
            name = event_item_dict.get("name")
            if not isinstance(item_id, str) or not item_id:
                raise RuntimeError("Responses function_call 缺少 item id")
            if not isinstance(call_id, str) or not call_id:
                raise RuntimeError(
                    f"Responses function_call 缺少 call_id: item_id={item_id}"
                )
            if not isinstance(name, str) or not name:
                raise RuntimeError(
                    f"Responses function_call 缺少工具名: call_id={call_id}"
                )
            arguments = event_item_dict.get("arguments")
            if not isinstance(arguments, str):
                arguments = ""
            part_state.responses_tool_call_ids_by_item_id[item_id] = call_id
            part_state.responses_tool_call_ids_by_output_index[output_index] = call_id
            return (
                output_index,
                output_index,
                current_sub_index,
                ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {
                                "type": "tool_call_chunk",
                                "name": name,
                                "args": arguments,
                                "id": call_id,
                                "index": output_index,
                            }
                        ],
                    )
                ),
            )

        if event_type == "response.function_call_arguments.delta":
            output_index = getattr(event, "output_index", None)
            if isinstance(output_index, bool) or not isinstance(output_index, int):
                raise RuntimeError(
                    "Responses function_call arguments delta 缺少有效 output_index"
                )
            item_id = getattr(event, "item_id", None)
            call_id = getattr(event, "call_id", None)
            if not isinstance(call_id, str) or not call_id:
                call_id = (
                    part_state.responses_tool_call_ids_by_item_id.get(item_id)
                    if isinstance(item_id, str)
                    else None
                )
            if not isinstance(call_id, str) or not call_id:
                call_id = part_state.responses_tool_call_ids_by_output_index.get(
                    output_index
                )
            if not isinstance(call_id, str) or not call_id:
                raise RuntimeError(
                    "Responses function_call arguments delta 无法关联 function_call: "
                    f"item_id={item_id!r} output_index={output_index}"
                )
            delta = getattr(event, "delta", None)
            if not isinstance(delta, str):
                raise RuntimeError(
                    f"Responses function_call arguments delta 必须是字符串: call_id={call_id}"
                )
            tool_chunk: dict[str, Any] = {
                "type": "tool_call_chunk",
                "args": delta,
                "id": call_id,
                "index": output_index,
            }
            return (
                output_index,
                output_index,
                current_sub_index,
                ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=[tool_chunk],
                    )
                ),
            )

        if (
            event_type
            in {
                "response.function_call_arguments.done",
                "response.output_item.done",
            }
            and event_item_dict.get("type") == "function_call"
        ):
            return current_index, current_output_index, current_sub_index, None

        if event_type in {
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_text.delta",
            "response.reasoning_summary_part.done",
        }:
            provider_item_id = getattr(event, "item_id", None)
            if isinstance(provider_item_id, str):
                part_state.reasoning_summary_provider_ids.add(provider_item_id)
            if event_type == "response.reasoning_summary_text.delta":
                delta = getattr(event, "delta", None)
                if not isinstance(delta, str) or not delta:
                    return (
                        current_index,
                        current_output_index,
                        current_sub_index,
                        None,
                    )
                block: dict[str, Any] = {
                    "type": "reasoning",
                    "content": [
                        {
                            "type": "reasoning_text",
                            "text": delta,
                        }
                    ],
                }
                if isinstance(provider_item_id, str):
                    block["id"] = provider_item_id
                chunk = ChatGenerationChunk(
                    message=AIMessageChunk(content=[part_state.decorate(block)])
                )
                return (
                    current_index,
                    current_output_index,
                    current_sub_index,
                    chunk,
                )

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
        delta_sink = get_current_model_delta_sink()
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
            if (
                self._message_chunk_has_semantic_delta(generation_chunk.message)
                and delta_sink is not None
            ):
                raise RuntimeError(
                    "同步 Responses 模型流不能承载异步消息流 delta hook；"
                    "AgentLoop 必须使用异步模型流"
                )
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
        # 历史投影和 Responses payload 构造可能遍历很长的会话/附件记录。
        # 不能在 Agent 事件循环中同步执行，否则首个模型事件前会阻塞
        # watchdog、heartbeat 与 SSE；将其放到线程中，但仍由上层有限预算收敛。
        payload_deadline = (
            time.monotonic() + DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS
        )
        try:
            raw_payload = await asyncio.wait_for(
                asyncio.to_thread(
                    self._responses_payload,
                    messages,
                    stop,
                    kwargs,
                    deadline=payload_deadline,
                ),
                timeout=DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS,
            )
        except TimeoutError as error:
            logger.warning(
                "Responses provider payload build timeout: provider=%s model=%s "
                "timeout_seconds=%s",
                self.provider_id or self.custom_llm_provider or "<unknown>",
                self.model,
                DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS,
            )
            if isinstance(error, ResponsesPayloadBuildTimeoutError):
                raise
            raise ResponsesPayloadBuildTimeoutError(
                "Responses 请求 payload 构造超过时间预算: "
                f"timeout_seconds={DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS:g}"
            ) from error
        payload = attach_upstream_trace_callback(
            raw_payload,
            fallback_request=raw_payload,
        )
        provider_id = self.provider_id or self.custom_llm_provider or "<unknown>"
        logger.info(
            "Responses provider stream open begin: provider=%s model=%s",
            provider_id,
            self.model,
        )
        try:
            stream = await asyncio.wait_for(
                litellm.aresponses(**payload),
                timeout=DEFAULT_RESPONSES_STREAM_OPEN_TIMEOUT_SECONDS,
            )
        except TimeoutError as error:
            logger.warning(
                "Responses provider stream open timeout: provider=%s model=%s "
                "timeout_seconds=%s",
                provider_id,
                self.model,
                DEFAULT_RESPONSES_STREAM_OPEN_TIMEOUT_SECONDS,
            )
            raise ResponsesStreamOpenTimeoutError(
                "Responses provider 在有限时间内未建立事件流: "
                f"provider={provider_id} model={self.model} "
                f"timeout_seconds={DEFAULT_RESPONSES_STREAM_OPEN_TIMEOUT_SECONDS:g}"
            ) from error
        logger.info(
            "Responses provider stream open complete: provider=%s model=%s",
            provider_id,
            self.model,
        )
        original_schema = kwargs.get("response_format")
        current_index = current_output_index = current_sub_index = -1
        part_state = _StreamPartState()
        delta_sink = get_current_model_delta_sink()
        raw_model_call_id = getattr(run_manager, "run_id", None)
        model_call_id = str(raw_model_call_id) if raw_model_call_id is not None else None
        scope = get_current_turn_execution_scope()
        signal = scope.effective_cancellation_signal if scope else None

        async def close_response_stream() -> None:
            await close_async_stream(stream)
            response = getattr(stream, "response", None)
            if response is not None and response is not stream:
                await close_async_stream(response)

        async with CancelableStream(
            stream,
            signal,
            close_upstream_stream=close_response_stream,
        ) as cancelable_stream:
            async for event in cancelable_stream:
                if scope is not None:
                    scope.raise_if_cancelled()
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
                if (
                    self._message_chunk_has_semantic_delta(generation_chunk.message)
                    and delta_sink is not None
                ):
                    if model_call_id is None:
                        await delta_sink.accept_message_chunk(generation_chunk.message)
                    else:
                        await delta_sink.accept_message_chunk(
                            generation_chunk.message,
                            model_call_id=model_call_id,
                        )
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
        image_input_replay="image_input" in parse_provider_capabilities(provider),
        no_proxy=bool(request_options.get("no_proxy")),
    )
