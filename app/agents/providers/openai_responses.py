from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import Any, ClassVar

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
from app.agents.providers.wire_finalize import (
    finalize_responses_body,
    finalize_responses_wire,
    responses_send_params,
    responses_wire_profile,
    wire_request_params,
)
from app.agents.sealed_assembly_dispatch import (
    ProviderInvocationPurpose,
    read_sealed_assembly_dispatch,
    validate_sealed_assembly_dispatch,
)
from app.agents.upstream_request_trace import (
    apply_upstream_call_details,
    record_upstream_error,
    record_upstream_request,
    record_upstream_response,
)
from app.core.cancelable_stream import CancelableStream, close_async_stream
from app.core.model_delta_context import (
    get_current_model_delta_sink,
    resolve_model_run_id,
)
from app.core.turn_execution_scope import (
    ScopeCancelledError,
    get_current_turn_execution_scope,
)
from app.runtime.litellm_runtime import litellm, prepare_model_request
from app.runtime.provider_errors import mark_provider_failure
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
        binding = read_sealed_assembly_dispatch(invocation_purpose=self.invocation_purpose)
        payload = responses_send_params(self, kwargs=kwargs)
        payload.update({
            "stream": True,
            "include": list(self.responses_include),
            "store": self.responses_store,
        })
        if binding is not None:
            native = binding.native_projection
            if not isinstance(native, Mapping):
                raise TypeError("source-mismatch: Responses binding 缺少 native projection")
            request = native.get("request")
            if not isinstance(request, Mapping):
                raise TypeError("source-mismatch: native request 必须是 object")
            if "input" not in request or "tools" not in request:
                raise TypeError("source-mismatch: native request 缺少 input/tools")
            # 封存正文（input/tools）与参数合并、SDK Responses 参数映射、
            # reasoning 清洗与 chatgpt instructions 归并共用唯一 finalize。
            if deadline is not None and time.monotonic() >= deadline:
                raise ResponsesPayloadBuildTimeoutError(
                    "Responses 请求 payload 构造超过时间预算: "
                    f"timeout_seconds={DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS:g}"
                )
            return finalize_responses_wire(
                input_items=request["input"],
                tools=request["tools"],
                params=payload,
                include=payload["include"],
                store=payload["store"],
                merge_system_to_instructions=self.custom_llm_provider == "chatgpt",
            )
        # 只有构建时明确指定 tool_test 才能走原非 Agent 历史输入构造。
        finalized = finalize_responses_body(
            payload,
            messages=self._history_messages(messages, deadline=deadline),
            merge_system_to_instructions=self.custom_llm_provider == "chatgpt",
        )
        if deadline is not None and time.monotonic() >= deadline:
            raise ResponsesPayloadBuildTimeoutError(
                "Responses 请求 payload 构造超过时间预算: "
                f"timeout_seconds={DEFAULT_RESPONSES_PAYLOAD_BUILD_TIMEOUT_SECONDS:g}"
            )
        return finalized

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
            if response is None:
                raise RuntimeError(
                    f"Responses 流协议错误: {event_type} terminal event 缺少 response"
                )
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
            error = RuntimeError(f"LiteLLM Responses 请求失败: {event!r}")
            mark_provider_failure(error)
            record_upstream_error(error)
            raise error

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
        payload = self._responses_payload(messages, stop, kwargs)
        binding = read_sealed_assembly_dispatch(invocation_purpose=self.invocation_purpose)
        validate_sealed_assembly_dispatch(
            invocation_purpose=self.invocation_purpose,
            profile=responses_wire_profile(self),
            wire_request=wire_request_params(self, payload),
        )
        prepare_model_request()
        record_upstream_request(
            request=payload,
            model=self.model,
            provider=self.custom_llm_provider,
            api_base=self.api_base,
            call_type="responses",
            dispatch_ref=binding.reference if binding is not None else None,
            resource_refs=binding.resource_refs if binding is not None else (),
        )
        try:
            stream = litellm.responses(**payload)
        except ScopeCancelledError:
            raise
        except Exception as error:
            mark_provider_failure(error)
            record_upstream_error(error)
            raise
        apply_upstream_call_details(stream)
        current_index = current_output_index = current_sub_index = -1
        part_state = _StreamPartState()
        original_schema = kwargs.get("response_format")
        delta_sink = get_current_model_delta_sink()
        try:
            iterator = iter(stream)
        except ScopeCancelledError:
            raise
        except Exception as error:
            mark_provider_failure(error)
            record_upstream_error(error)
            raise
        terminal_received = False
        while True:
            try:
                event = next(iterator)
            except StopIteration:
                if not terminal_received:
                    error = RuntimeError(
                        "Responses 流协议错误: EOF 前未收到 "
                        "response.completed/response.incomplete terminal event"
                    )
                    mark_provider_failure(error)
                    record_upstream_error(error)
                    raise error
                break
            except ScopeCancelledError:
                raise
            except Exception as error:
                mark_provider_failure(error)
                record_upstream_error(error)
                raise
            try:
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
            except Exception as error:
                mark_provider_failure(error)
                record_upstream_error(error)
                raise
            if getattr(event, "type", None) in {"response.completed", "response.incomplete"}:
                terminal_received = True
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
        provider_id = self.provider_id or self.custom_llm_provider or "<unknown>"
        binding = read_sealed_assembly_dispatch(invocation_purpose=self.invocation_purpose)
        logger.info(
            "Responses provider stream open begin: provider=%s model=%s",
            provider_id,
            self.model,
        )
        validate_sealed_assembly_dispatch(
            invocation_purpose=self.invocation_purpose,
            profile=responses_wire_profile(self),
            wire_request=wire_request_params(self, raw_payload),
        )
        prepare_model_request()
        open_timeout = asyncio.timeout(DEFAULT_RESPONSES_STREAM_OPEN_TIMEOUT_SECONDS)
        record_upstream_request(
            request=raw_payload,
            model=self.model,
            provider=self.custom_llm_provider,
            api_base=self.api_base,
            call_type="aresponses",
            dispatch_ref=binding.reference if binding is not None else None,
            resource_refs=binding.resource_refs if binding is not None else (),
        )
        try:
            async with open_timeout:
                stream = await litellm.aresponses(**raw_payload)
        except TimeoutError as error:
            if not open_timeout.expired():
                mark_provider_failure(error)
                record_upstream_error(error)
                raise
            logger.warning(
                "Responses provider stream open timeout: provider=%s model=%s "
                "timeout_seconds=%s",
                provider_id,
                self.model,
                DEFAULT_RESPONSES_STREAM_OPEN_TIMEOUT_SECONDS,
            )
            timeout_error = ResponsesStreamOpenTimeoutError(
                "Responses provider 在有限时间内未建立事件流: "
                f"provider={provider_id} model={self.model} "
                f"timeout_seconds={DEFAULT_RESPONSES_STREAM_OPEN_TIMEOUT_SECONDS:g}"
            )
            record_upstream_error(timeout_error)
            raise timeout_error from error
        except ScopeCancelledError:
            raise
        except Exception as error:
            mark_provider_failure(error)
            record_upstream_error(error)
            raise
        apply_upstream_call_details(stream)
        logger.info(
            "Responses provider stream open complete: provider=%s model=%s",
            provider_id,
            self.model,
        )
        original_schema = kwargs.get("response_format")
        current_index = current_output_index = current_sub_index = -1
        part_state = _StreamPartState()
        delta_sink = get_current_model_delta_sink()
        model_call_id = resolve_model_run_id(delta_sink, run_manager)
        scope = get_current_turn_execution_scope()
        signal = scope.effective_cancellation_signal if scope else None

        async def close_response_stream() -> None:
            await close_async_stream(stream)
            response = getattr(stream, "response", None)
            if response is not None and response is not stream:
                await close_async_stream(response)

        try:
            cancelable_stream = CancelableStream(
                stream,
                signal,
                close_upstream_stream=close_response_stream,
            )
        except ScopeCancelledError:
            raise
        except Exception as error:
            mark_provider_failure(error)
            record_upstream_error(error)
            raise
        terminal_received = False
        async with cancelable_stream:
            iterator = aiter(cancelable_stream)
            while True:
                try:
                    event = await anext(iterator)
                except StopAsyncIteration:
                    if not terminal_received:
                        error = RuntimeError(
                            "Responses 流协议错误: EOF 前未收到 "
                            "response.completed/response.incomplete terminal event"
                        )
                        mark_provider_failure(error)
                        record_upstream_error(error)
                        raise error
                    break
                except ScopeCancelledError:
                    raise
                except Exception as error:
                    mark_provider_failure(error)
                    record_upstream_error(error)
                    raise
                if scope is not None:
                    scope.raise_if_cancelled()
                try:
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
                except Exception as error:
                    mark_provider_failure(error)
                    record_upstream_error(error)
                    raise
                if getattr(event, "type", None) in {"response.completed", "response.incomplete"}:
                    terminal_received = True
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
    invocation_purpose: ProviderInvocationPurpose = "agent",
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
        invocation_purpose=invocation_purpose,
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
