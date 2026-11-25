from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
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
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai.chat_models.base import (
    _construct_lc_result_from_responses_api,
    _convert_responses_chunk_to_generation_chunk,
)
from openai._models import construct_type_unchecked
from openai.types.responses import Response
from pydantic import Field

from app.agents.provider_api_mode import parse_provider_api_mode
from app.agents.provider_capabilities import parse_provider_capabilities
from app.agents.providers.litellm_chat import (
    BoxteamLiteLLMChatModel,
    _message_chunk_token,
)
from app.agents.providers.litellm_stream_types import (
    _as_dict,
    _ResponsesToolCall,
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
from app.agents.providers.request_parameters import merge_request_parameters
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
from app.core.failures import mark_provider_failure
from app.core.model_delta_context import (
    get_current_model_delta_sink,
    resolve_model_run_id,
)
from app.core.turn_execution_scope import (
    ScopeCancelledError,
    get_current_turn_execution_scope,
)
from app.runtime.litellm_runtime import litellm, prepare_model_request
from app.runtime.network_proxy import ProxyRoutes
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

    chatgpt_auth_directory: Path | None = Field(default=None, exclude=True, repr=False)
    auth_proxy_routes: ProxyRoutes | None = Field(default=None, exclude=True, repr=False)

    _uses_http_handler: ClassVar[bool] = True
    responses_include: ClassVar[list[str]] = ["reasoning.encrypted_content"]
    responses_store: bool = False
    reasoning_items_summary_replay: bool = False
    reasoning_items_encrypted_replay: bool = False
    image_input_replay: bool = False

    @property
    def _default_params(self) -> dict[str, Any]:
        """Responses 不继承 Chat 的 n/max_tokens 等参数别名和默认值。"""
        return {
            "model": self.model_name or self.model,
            "timeout": self.request_timeout,
            "custom_llm_provider": self.custom_llm_provider,
            **self.model_kwargs,
        }

    def _history_messages(
        self,
        messages: Sequence[BaseMessage],
        *,
        deadline: float | None = None,
    ) -> list[BaseMessage]:
        normalized_messages = _project_tool_history(messages, deadline=deadline)
        target_capabilities = responses_wire_profile(self).replay_capabilities()
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
                target_source=self.model_source,
                target_capabilities=target_capabilities,
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
            reasoning_content: list[Any] = []
            visible_content: list[Any] = []
            for block in prepared_content:
                if isinstance(block, dict) and block.get("type") == "reasoning":
                    reasoning_content.append(block)
                else:
                    visible_content.append(block)
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
            "stream": self.streaming,
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
            unfinished = [call.call_id for call in part_state.responses_tool_calls.values() if not call.completed]
            if unfinished:
                raise RuntimeError(f"Responses 流协议错误: terminal event 到达时工具参数未完成: {unfinished}")
            record_upstream_response(response)
            usage = getattr(response, "usage", None)
            metadata = {
                "model_provider": "litellm",
                "custom_llm_provider": self.custom_llm_provider,
                "provider_id": self.provider_id,
                "model_source": self.model_source.to_dict() if self.model_source is not None else None,
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
            if output_index in part_state.responses_tool_calls:
                raise RuntimeError(f"Responses function_call 重复 output_index: {output_index}")
            part_state.responses_tool_calls[output_index] = _ResponsesToolCall(item_id, call_id, name, arguments)
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
                                "args": "",
                                "id": call_id,
                                "index": output_index,
                            }
                        ],
                    )
                ),
            )

        if event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"} or (
            event_type == "response.output_item.done" and event_item_dict.get("type") == "function_call"
        ):
            output_index = getattr(event, "output_index", None)
            if isinstance(output_index, bool) or not isinstance(output_index, int):
                raise RuntimeError(
                    "Responses function_call 参数事件缺少有效 output_index"
                )
            call = part_state.responses_tool_calls.get(output_index)
            if call is None:
                raise RuntimeError(f"Responses 参数事件无法关联 function_call: output_index={output_index}")
            item_id = event_item_dict.get("id", getattr(event, "item_id", None))
            call_id = event_item_dict.get("call_id", getattr(event, "call_id", None))
            if (item_id is not None and item_id != call.item_id) or (call_id is not None and call_id != call.call_id):
                raise RuntimeError(f"Responses function_call 参数事件身份冲突: output_index={output_index}")
            if event_item_dict.get("name", call.name) != call.name:
                raise RuntimeError(f"Responses function_call 完成工具名冲突: call_id={call.call_id}")
            value = getattr(event, "delta", None) if event_type.endswith(".delta") else event_item_dict.get(
                "arguments", getattr(event, "arguments", None),
            )
            if not isinstance(value, str):
                raise RuntimeError(f"Responses function_call 参数必须是字符串: call_id={call.call_id}")
            delta = call.append(value) if event_type.endswith(".delta") else call.finish(value)
            if not delta:
                return current_index, current_output_index, current_sub_index, None
            tool_chunk: dict[str, Any] = {
                "type": "tool_call_chunk",
                "args": delta,
                "id": call.call_id,
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

    def _complete_response(self, response: object, schema: Any) -> ChatResult:
        """完整响应复用官方消息／工具／usage 转换，再进入原 canonical 归一化。"""
        body = _as_dict(response)
        if body.get("status") not in {"completed", "incomplete"}:
            raise ValueError(f"Responses 完整响应缺少有效终态: {body.get('status')!r}")
        # 采用 OpenAI 客户端自身的响应解码，避免在 LiteLLM 解码后追加更严的重校验。
        # TODO: OpenAI 提供公共响应解码 API 后替换这个 SDK 私有入口。
        parsed = construct_type_unchecked(value=body, type_=Response)
        # TODO: langchain-openai 提供公共完整响应转换 API 后替换私有 helper。
        result = _construct_lc_result_from_responses_api(parsed, schema=schema, output_version="responses/v1")
        record_upstream_response(body)
        return self._canonicalize_chat_result(result)

    def _record_responses_request(self, payload: dict[str, Any], *, asynchronous: bool) -> None:
        binding = read_sealed_assembly_dispatch(invocation_purpose=self.invocation_purpose)
        validate_sealed_assembly_dispatch(
            invocation_purpose=self.invocation_purpose,
            profile=responses_wire_profile(self),
            wire_request=wire_request_params(self, payload),
        )
        prepare_model_request()
        record_upstream_request(
            request=payload, model=self.model, provider=self.custom_llm_provider,
            api_base=self.api_base, call_type="aresponses" if asynchronous else "responses",
            dispatch_ref=binding.reference if binding is not None else None,
            resource_refs=binding.resource_refs if binding is not None else (),
        )
        client = self._provider_http_client(is_async=asynchronous)
        payload["client"] = client
        if not asynchronous:
            self._prepare_oauth_request(payload)
        # Responses 没有 LangChain completion 重试层，直接消费构造时的实际值。
        payload["max_retries"] = payload["num_retries"] = self.max_retries

    def _prepare_oauth_request(self, payload: dict[str, Any]) -> None:
        if self.chatgpt_auth_directory is None:
            return
        from app.runtime.chatgpt_auth import prepare_chatgpt_oauth

        if self.auth_proxy_routes is None:
            raise ValueError("ChatGPT OAuth 缺少所属 Provider 的网络路由")
        credential = prepare_chatgpt_oauth(self.chatgpt_auth_directory, routes=self.auth_proxy_routes)
        headers = {key: value for key, value in (payload.get("extra_headers") or {}).items()
                   if key.casefold() not in {"authorization", "chatgpt-account-id"}}
        headers["Authorization"] = "Bearer " + credential.access_token
        if credential.account_id:
            headers["ChatGPT-Account-ID"] = credential.account_id
        payload["api_key"], payload["extra_headers"] = credential.access_token, headers

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None, stream: bool | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if stream is not None and stream != self.streaming:
            raise ValueError("stream 不能改写已配置的 api_mode.streaming")
        if self.streaming:
            return super()._generate(messages, stop=stop, run_manager=run_manager, stream=stream, **kwargs)
        payload = self._responses_payload(messages, stop, kwargs)
        self._record_responses_request(payload, asynchronous=False)
        try:
            response = litellm.responses(**payload)
            return self._complete_response(response, kwargs.get("response_format"))
        except ScopeCancelledError:
            raise
        except Exception as error:
            mark_provider_failure(error, secrets=(self.api_key, payload.get("api_key")), diagnostic_logger=logger)
            record_upstream_error(error)
            raise

    async def _agenerate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, stream: bool | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if stream is not None and stream != self.streaming:
            raise ValueError("stream 不能改写已配置的 api_mode.streaming")
        if self.streaming:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, stream=stream, **kwargs)
        payload = self._responses_payload(messages, stop, kwargs)
        self._record_responses_request(payload, asynchronous=True)
        if self.chatgpt_auth_directory is not None:
            await asyncio.to_thread(self._prepare_oauth_request, payload)
        try:
            response = await self._await_nonstream_response(litellm.aresponses(**payload))
            result = self._complete_response(response, kwargs.get("response_format"))
        except ScopeCancelledError:
            raise
        except Exception as error:
            mark_provider_failure(error, secrets=(self.api_key, payload.get("api_key")), diagnostic_logger=logger)
            record_upstream_error(error)
            raise
        await self._publish_nonstream_result(result, run_manager)
        return result

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        payload = self._responses_payload(messages, stop, kwargs)
        self._record_responses_request(payload, asynchronous=False)
        try:
            stream = litellm.responses(**payload)
        except ScopeCancelledError:
            raise
        except Exception as error:
            mark_provider_failure(error, secrets=(self.api_key, payload.get("api_key")), diagnostic_logger=logger)
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
            mark_provider_failure(error, secrets=(self.api_key, payload.get("api_key")), diagnostic_logger=logger)
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
                    mark_provider_failure(error, secrets=(self.api_key, payload.get("api_key")), diagnostic_logger=logger)
                    record_upstream_error(error)
                    raise error
                break
            except ScopeCancelledError:
                raise
            except Exception as error:
                mark_provider_failure(error, secrets=(self.api_key, payload.get("api_key")), diagnostic_logger=logger)
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
                mark_provider_failure(error, secrets=(self.api_key, payload.get("api_key")), diagnostic_logger=logger)
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
        logger.info(
            "Responses provider stream open begin: provider=%s model=%s",
            provider_id,
            self.model,
        )
        self._record_responses_request(raw_payload, asynchronous=True)
        if self.chatgpt_auth_directory is not None:
            await asyncio.to_thread(self._prepare_oauth_request, raw_payload)
        open_timeout = asyncio.timeout(DEFAULT_RESPONSES_STREAM_OPEN_TIMEOUT_SECONDS)
        try:
            async with open_timeout:
                stream = await litellm.aresponses(**raw_payload)
        except TimeoutError as error:
            if not open_timeout.expired():
                mark_provider_failure(error, secrets=(self.api_key, raw_payload.get("api_key")), diagnostic_logger=logger)
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
            mark_provider_failure(error, secrets=(self.api_key, raw_payload.get("api_key")), diagnostic_logger=logger)
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
            mark_provider_failure(error, secrets=(self.api_key, raw_payload.get("api_key")), diagnostic_logger=logger)
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
                        mark_provider_failure(error, secrets=(self.api_key, raw_payload.get("api_key")), diagnostic_logger=logger)
                        record_upstream_error(error)
                        raise error
                    break
                except ScopeCancelledError:
                    raise
                except Exception as error:
                    mark_provider_failure(error, secrets=(self.api_key, raw_payload.get("api_key")), diagnostic_logger=logger)
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
                    mark_provider_failure(error, secrets=(self.api_key, raw_payload.get("api_key")), diagnostic_logger=logger)
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
    chatgpt_auth_directory: Path | None = None,
) -> BoxteamOpenAIResponsesModel:
    api_mode = parse_provider_api_mode(provider)
    if api_mode.protocol != "responses":
        raise ValueError(
            "build_openai_responses_model 只接受 api_mode.protocol='responses'"
        )
    if api_mode.streaming and not litellm.utils.supports_native_streaming(
        model=provider["model"], custom_llm_provider=provider["custom_llm_provider"],
    ):
        # 显式选择流式就必须发送流式；SDK 对公共目录未知的模型会擅自改成完整响应再伪造事件。
        # TODO: LiteLLM 提供逐调用禁用 fake stream 的选项后，替换这项 SDK 内存注册。
        prefix = provider["custom_llm_provider"] + "/"
        litellm.register_model({prefix + provider["model"].removeprefix(prefix): {
            "litellm_provider": provider["custom_llm_provider"], "supports_native_streaming": True,
        }})
    request_parameters = merge_request_parameters(
        request_options.get("parameters", {}),
        {key: runtime_config[key] for key in ("temperature", "top_p", "max_output_tokens") if key in runtime_config},
        protocol=api_mode.protocol,
    )
    if provider["custom_llm_provider"] == "chatgpt":
        if prompt_cache_key is not None:
            request_parameters["litellm_session_id"] = prompt_cache_key
    elif api_mode.request_features.prompt_cache_key:
        request_parameters["prompt_cache_key"] = (
            prompt_cache_key or f"boxteam:{provider['provider_id']}:{provider['model_id']}"
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
        max_retries=0 if invocation_purpose in {"model_test", "derived", "compaction"} else 3,
        streaming=api_mode.streaming,
        disable_streaming=not api_mode.streaming,
        model_kwargs=request_parameters,
        provider_id=provider.get("provider_id"),
        responses_include=responses_include,
        reasoning_items_summary_replay=api_mode.supports_reasoning.reasoning_items_summary,
        reasoning_items_encrypted_replay=api_mode.supports_reasoning.reasoning_items_encrypted_content,
        image_input_replay="image_input" in parse_provider_capabilities(provider),
        proxy_routes=provider["proxy_routes"],
        chatgpt_auth_directory=chatgpt_auth_directory,
        auth_proxy_routes=provider.get("auth_proxy_routes"),
    )
