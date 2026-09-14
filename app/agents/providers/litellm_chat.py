from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_litellm import ChatLiteLLM
from pydantic import PrivateAttr

from app.agents.provider_api_mode import parse_provider_api_mode
from app.agents.provider_capabilities import (
    PROMPT_CACHE_KEY,
    parse_provider_capabilities,
)
from app.agents.providers._format_check import (
    FormatCheckItem,
    FormatCheckResult,
    check_history_messages_accepted,
    validate_provider_format,
)
from app.agents.providers.litellm_history_projection import (
    LiteLLMHistoryProjectionMixin,
)
from app.agents.providers.litellm_stream_types import (
    _as_dict,
    _close_sync_stream,
    _create_usage_metadata,
    _message_chunk_token,
    _streamed_response_payload,
    _StreamPartState,
)
from app.agents.providers.output_normalization import (
    MISSING,
    build_ai_message_content,
)
from app.agents.providers.provider_http_client import (
    build_no_proxy_handler,
    build_no_proxy_openai_client,
)
from app.agents.providers.response_normalization import canonicalize_ai_message
from app.agents.upstream_request_trace import (
    attach_upstream_trace_callback,
    record_upstream_response,
)
from app.core.cancelable_stream import CancelableStream
from app.core.model_delta_context import get_current_model_delta_sink
from app.core.turn_execution_scope import get_current_turn_execution_scope


class BoxteamLiteLLMChatModel(LiteLLMHistoryProjectionMixin, ChatLiteLLM):
    """LiteLLM 模型包装层，统一输出 LangChain 标准 content blocks。"""

    provider_id: str | None = None
    reasoning_content_replay: bool = False
    thinking_blocks_replay: bool = False
    image_input_replay: bool = False
    # Provider 级免代理开关：为该 Provider 注入 trust_env=False 的 HTTP client。
    no_proxy: bool = False

    _no_proxy_sync_client: Any = PrivateAttr(default=None)
    _no_proxy_async_client: Any = PrivateAttr(default=None)

    def _provider_http_client(self, *, is_async: bool) -> Any:
        """返回本 Provider 的免代理 HTTP client；未启用时返回 None。

        Anthropic Messages 适配族要求 LiteLLM 的 HTTPHandler，Chat
        Completions / Responses 适配族要求 OpenAI SDK client，因此按协议分别
        构造。client 在本实例内缓存复用，避免每次请求重建连接池。
        """
        if not self.no_proxy:
            return None
        if is_async:
            if self._no_proxy_async_client is None:
                self._no_proxy_async_client = (
                    build_no_proxy_handler(is_async=True)
                    if self.custom_llm_provider == "anthropic"
                    else build_no_proxy_openai_client(
                        is_async=True,
                        base_url=self.api_base,
                        api_key=self.api_key,
                    )
                )
            return self._no_proxy_async_client
        if self._no_proxy_sync_client is None:
            self._no_proxy_sync_client = (
                build_no_proxy_handler(is_async=False)
                if self.custom_llm_provider == "anthropic"
                else build_no_proxy_openai_client(
                    is_async=False,
                    base_url=self.api_base,
                    api_key=self.api_key,
                )
            )
        return self._no_proxy_sync_client

    def completion_with_retry(
        self,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Any:
        client = self._provider_http_client(is_async=False)
        if client is not None:
            kwargs.setdefault("client", client)
        return super().completion_with_retry(run_manager=run_manager, **kwargs)

    async def acompletion_with_retry(
        self,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Any:
        client = self._provider_http_client(is_async=True)
        if client is not None:
            kwargs.setdefault("client", client)
        return await super().acompletion_with_retry(
            run_manager=run_manager, **kwargs
        )

    def _stream_attempt_count(self) -> int:
        """返回包含首次请求在内的流式请求总尝试次数。"""
        return int(self.max_retries or 0) + 1

    @staticmethod
    def _has_real_stream_termination(raw_stream: Any) -> bool:
        """判断 LiteLLM 是否从真实上游收到终止原因。

        LiteLLM 会在底层迭代器直接 EOF 时合成一个 finish_reason="stop" chunk，
        因此不能检查转换后的 chunk；只有 wrapper 记录的终止原因能区分真实终止
        与合成终止。
        """
        # TODO: LiteLLM 提供公开的“真实终止”标记后，替换对 wrapper 状态字段的读取。
        return bool(
            getattr(raw_stream, "received_finish_reason", None)
            or getattr(raw_stream, "intermittent_finish_reason", None)
        )

    def _incomplete_stream_error(self, attempts: int) -> RuntimeError:
        provider = self.provider_id or self.custom_llm_provider or "<unknown>"
        model = self.model_name or self.model
        return RuntimeError(
            "模型流在上游返回真实 finish_reason 前提前结束；"
            f"provider={provider}，model={model}，已尝试 {attempts} 次。"
            "已经收到的半截 delta 不会静默重试，AgentLoop 必须将本次调用标记为失败。"
        )

    def _create_message_dicts(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        params = {
            key: value
            for key, value in self._client_params.items()
            if value is not None
        }
        if stop is not None:
            if "stop" in params:
                raise ValueError("`stop` 同时出现在输入和默认参数中")
            params["stop"] = stop
        return self._convert_messages_to_dicts(messages), params

    def _delta_reasoning(self, delta: Mapping[str, Any]) -> str:
        for key in ("reasoning_content", "reasoning"):
            value = delta.get(key)
            if isinstance(value, str) and value:
                return value
        model_extra = delta.get("model_extra")
        if isinstance(model_extra, dict):
            for key in ("reasoning_content", "reasoning"):
                value = model_extra.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    def _delta_reasoning_blocks(
        self,
        delta: Mapping[str, Any],
        *,
        part_state: _StreamPartState,
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        thinking_blocks = delta.get("thinking_blocks")
        if isinstance(thinking_blocks, list):
            for raw_block in thinking_blocks:
                block = _as_dict(raw_block)
                if block.get("type") not in {"thinking", "redacted_thinking"}:
                    continue
                # thinking/redacted_thinking 已经是 LiteLLM 的完整 provider
                # block，直接作为 AIMessage.content 的一部分传递。
                blocks.append(block)
        reasoning_items = delta.get("reasoning_items")
        if isinstance(reasoning_items, list):
            items: list[dict[str, Any]] = []
            for index, raw_item in enumerate(reasoning_items):
                item = _as_dict(raw_item)
                if not item:
                    continue
                if not isinstance(item.get("id"), str) or not item["id"]:
                    item["id"] = part_state.item_id(index)
                items.append(item)
            if items:
                blocks.append(
                    {
                        "type": "reasoning_items",
                        "reasoning_items": items,
                    }
                )
        model_extra = delta.get("model_extra")
        if isinstance(model_extra, Mapping):
            blocks.extend(
                self._delta_reasoning_blocks(model_extra, part_state=part_state)
            )
        return blocks

    def _delta_tool_call_chunks(self, raw_tool_calls: Any) -> list[dict[str, Any]]:
        tool_call_chunks: list[dict[str, Any]] = []
        if not raw_tool_calls:
            return tool_call_chunks

        for raw_tool_call in raw_tool_calls:
            tool_call = _as_dict(raw_tool_call)
            function = _as_dict(tool_call.get("function"))
            tool_call_chunks.append(
                {
                    "name": function.get("name"),
                    "args": function.get("arguments"),
                    "id": tool_call.get("id"),
                    "index": tool_call.get("index"),
                }
            )
        return tool_call_chunks

    def _stream_content(
        self,
        content: Any,
        *,
        part_state: _StreamPartState,
    ) -> Any:
        normalized = self.normalize_output_content(content)
        if not isinstance(normalized, list):
            return normalized
        return [
            part_state.decorate(block) if isinstance(block, dict) else block
            for block in normalized
        ]

    def _delta_to_message_chunks(
        self,
        delta: Mapping[str, Any],
        *,
        part_state: _StreamPartState,
    ) -> list[AIMessageChunk]:
        chunks: list[AIMessageChunk] = []
        reasoning = self._delta_reasoning(delta)
        if reasoning and part_state.accept_reasoning_alias(
            "reasoning_content", reasoning
        ):
            chunks.append(
                AIMessageChunk(
                    content=self._stream_content(
                        [
                            {
                                "type": "reasoning_content",
                                "reasoning_content": reasoning,
                            }
                        ],
                        part_state=part_state,
                    ),
                )
            )

        structured_reasoning = self._delta_reasoning_blocks(
            delta,
            part_state=part_state,
        )
        for index, block in enumerate(structured_reasoning):
            block_type = block.get("type")
            block_text = block.get("thinking")
            if block_type != "thinking" or not isinstance(block_text, str) or not block_text:
                part_state.reset_reasoning_alias()
            if (
                block_type == "thinking"
                and isinstance(block_text, str)
                and block_text
                and not part_state.accept_reasoning_alias("thinking", block_text)
            ):
                continue
            if index:
                part_state.close()
            chunks.append(
                AIMessageChunk(
                    content=[part_state.decorate(block)],
                )
            )

        content = delta.get("content")
        if content:
            part_state.reset_reasoning_alias()
            chunks.append(
                AIMessageChunk(
                    content=self._stream_content(content, part_state=part_state),
                )
            )

        raw_tool_calls = delta.get("tool_calls")
        tool_call_chunks = self._delta_tool_call_chunks(raw_tool_calls)
        if tool_call_chunks:
            part_state.reset_reasoning_alias()
            part_state.close()
            chunks.append(
                AIMessageChunk(
                    content="",
                    additional_kwargs={"tool_calls": raw_tool_calls},
                    tool_call_chunks=tool_call_chunks,  # type: ignore[arg-type]
                )
            )

        provider_specific_fields = delta.get("provider_specific_fields") or delta.get(
            "vertex_ai_grounding_metadata"
        )
        if provider_specific_fields is not None:
            chunks.append(
                AIMessageChunk(
                    content="",
                    additional_kwargs={
                        "provider_specific_fields": provider_specific_fields
                    },
                )
            )

        return chunks

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        message_dicts, params = self._create_message_dicts(messages, stop)
        params = {**params, **kwargs, "stream": True}
        params = attach_upstream_trace_callback(params)
        params["stream_options"] = self.stream_options or {"include_usage": True}

        delta_sink = get_current_model_delta_sink()
        attempts = self._stream_attempt_count()
        for attempt in range(1, attempts + 1):
            first_chunk_yielded = False
            part_state = _StreamPartState()
            semantic_delta_seen = False
            raw_stream = self.completion_with_retry(
                messages=message_dicts,
                run_manager=run_manager,
                **params,
            )
            scope = get_current_turn_execution_scope()
            hook_id = None
            if scope is not None:
                hook_id = scope.effective_cancellation_signal.add_hook(
                    lambda _reason, stream=raw_stream: _close_sync_stream(stream)
                )
            try:
                for raw_chunk in raw_stream:
                    if scope is not None:
                        scope.raise_if_cancelled()
                    for cg_chunk in self._convert_stream_response_chunk(
                        raw_chunk,
                        first_chunk_yielded=first_chunk_yielded,
                        part_state=part_state,
                    ):
                        if self._message_chunk_has_semantic_delta(cg_chunk.message):
                            semantic_delta_seen = True
                            if delta_sink is not None:
                                raise RuntimeError(
                                    "同步 LiteLLM 模型流不能承载异步消息流 delta hook；"
                                    "AgentLoop 必须使用异步模型流"
                                )
                        first_chunk_yielded = True
                        if run_manager:
                            run_manager.on_llm_new_token(
                                _message_chunk_token(cg_chunk.message),
                                chunk=cg_chunk,
                            )
                        yield cg_chunk
            finally:
                if scope is not None and hook_id is not None:
                    scope.cancellation_signal.remove_hook(hook_id)
                _close_sync_stream(raw_stream)
            if scope is not None:
                scope.raise_if_cancelled()
            if self._has_real_stream_termination(raw_stream):
                return
            if semantic_delta_seen:
                raise self._incomplete_stream_error(attempt)
        raise self._incomplete_stream_error(attempts)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        message_dicts, params = self._create_message_dicts(messages, stop)
        params = {**params, **kwargs, "stream": True}
        params = attach_upstream_trace_callback(params)
        params["stream_options"] = self.stream_options or {"include_usage": True}

        delta_sink = get_current_model_delta_sink()
        raw_model_call_id = getattr(run_manager, "run_id", None)
        model_call_id = str(raw_model_call_id) if raw_model_call_id is not None else None
        attempts = self._stream_attempt_count()
        for attempt in range(1, attempts + 1):
            first_chunk_yielded = False
            part_state = _StreamPartState()
            semantic_delta_seen = False
            streamed_chunks: list[AIMessageChunk] = []
            raw_stream = await self.acompletion_with_retry(
                messages=message_dicts,
                run_manager=run_manager,
                **params,
            )
            scope = get_current_turn_execution_scope()
            signal = scope.effective_cancellation_signal if scope else None
            async with CancelableStream(raw_stream, signal) as cancelable_stream:
                async for raw_chunk in cancelable_stream:
                    if scope is not None:
                        scope.raise_if_cancelled()
                    for cg_chunk in self._convert_stream_response_chunk(
                        raw_chunk,
                        first_chunk_yielded=first_chunk_yielded,
                        part_state=part_state,
                    ):
                        streamed_chunks.append(cg_chunk.message)
                        if self._message_chunk_has_semantic_delta(cg_chunk.message):
                            semantic_delta_seen = True
                            if delta_sink is not None:
                                if model_call_id is None:
                                    await delta_sink.accept_message_chunk(
                                        cg_chunk.message
                                    )
                                else:
                                    await delta_sink.accept_message_chunk(
                                        cg_chunk.message,
                                        model_call_id=model_call_id,
                                    )
                        first_chunk_yielded = True
                        if run_manager:
                            await run_manager.on_llm_new_token(
                                _message_chunk_token(cg_chunk.message),
                                chunk=cg_chunk,
                            )
                        yield cg_chunk
            if scope is not None:
                scope.raise_if_cancelled()
            if self._has_real_stream_termination(raw_stream):
                record_upstream_response(_streamed_response_payload(streamed_chunks))
                return
            if semantic_delta_seen:
                raise self._incomplete_stream_error(attempt)
        raise self._incomplete_stream_error(attempts)

    @staticmethod
    def _message_chunk_has_semantic_delta(message: AIMessageChunk) -> bool:
        content = getattr(message, "content", None)
        if isinstance(content, str) and content:
            return True
        if isinstance(content, list) and any(content):
            return True
        return bool(getattr(message, "tool_call_chunks", None))

    def _convert_stream_response_chunk(
        self,
        raw_chunk: Any,
        *,
        first_chunk_yielded: bool,
        part_state: _StreamPartState,
    ) -> list[ChatGenerationChunk]:
        chunk = _as_dict(raw_chunk)
        usage_metadata = None
        if chunk.get("usage"):
            usage_metadata = _create_usage_metadata(chunk["usage"])

        choices = chunk.get("choices") or []
        if not choices:
            if usage_metadata is None:
                return []
            message_chunk = AIMessageChunk(content="", usage_metadata=usage_metadata)
            return [ChatGenerationChunk(message=message_chunk)]

        choice = _as_dict(choices[0])
        delta = _as_dict(choice.get("delta"))
        if chunk.get("provider_specific_fields"):
            delta["provider_specific_fields"] = chunk["provider_specific_fields"]
        elif chunk.get("vertex_ai_grounding_metadata"):
            delta["vertex_ai_grounding_metadata"] = chunk[
                "vertex_ai_grounding_metadata"
            ]

        result: list[ChatGenerationChunk] = []
        for message_chunk in self._delta_to_message_chunks(
            delta,
            part_state=part_state,
        ):
            if usage_metadata:
                message_chunk.usage_metadata = usage_metadata
            if not first_chunk_yielded:
                message_chunk.response_metadata = {
                    "model_name": self.model_name or self.model,
                    "model_provider": "litellm",
                    "custom_llm_provider": self.custom_llm_provider,
                    "provider_id": self.provider_id,
                }
                first_chunk_yielded = True
            result.append(ChatGenerationChunk(message=message_chunk))
        if usage_metadata is not None and not result:
            result.append(
                ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        usage_metadata=usage_metadata,
                    )
                )
            )
        return result

    def _canonicalize_chat_result(self, result: ChatResult) -> ChatResult:
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

    def _generate_with_cache(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """覆盖 LangChain streaming=True 时绕过 _generate 的合并入口。"""
        result = super()._generate_with_cache(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )
        return self._canonicalize_chat_result(result)

    async def _agenerate_with_cache(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """覆盖异步 streaming=True 时绕过 _agenerate 的合并入口。"""
        result = await super()._agenerate_with_cache(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )
        return self._canonicalize_chat_result(result)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        stream: bool | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = super()._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            stream=stream,
            **kwargs,
        )
        return self._canonicalize_chat_result(result)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        stream: bool | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = await super()._agenerate(
            messages,
            stop=stop,
            run_manager=run_manager,
            stream=stream,
            **kwargs,
        )
        return self._canonicalize_chat_result(result)

    def _create_chat_result(self, response: Mapping[str, Any]) -> ChatResult:
        result = super()._create_chat_result(response)
        generations: list[ChatGeneration] = []
        raw_choices = response.get("choices") or []
        for generation_index, generation in enumerate(result.generations):
            message = generation.message
            if isinstance(message, AIMessage):
                raw_choice = (
                    _as_dict(raw_choices[generation_index])
                    if generation_index < len(raw_choices)
                    else {}
                )
                raw_message = _as_dict(raw_choice.get("message"))
                additional_kwargs = dict(message.additional_kwargs or {})
                # 这里只读取 LiteLLM/LangChain 响应对象的临时 wire 字段，
                # 立即转换为直接 content；它们不会作为消息的第二份来源保存。
                raw_content = raw_message.get("content", message.content)
                reasoning_content = raw_message.get(
                    "reasoning_content",
                    additional_kwargs.get("reasoning_content", MISSING),
                )
                thinking_blocks = raw_message.get(
                    "thinking_blocks",
                    additional_kwargs.get("thinking_blocks", MISSING),
                )
                reasoning_items = raw_message.get(
                    "reasoning_items",
                    additional_kwargs.get("reasoning_items", MISSING),
                )
                canonical_content = build_ai_message_content(
                    raw_content,
                    source_provider=self.provider_id,
                    source_model=self.model_name or self.model,
                    reasoning_content=reasoning_content,
                    thinking_blocks=thinking_blocks,
                    reasoning_items=reasoning_items,
                )
                for key in (
                    "reasoning_content",
                    "thinking_blocks",
                    "reasoning_items",
                ):
                    additional_kwargs.pop(key, None)
                message = message.model_copy(
                    update={
                        "content": canonical_content,
                        "additional_kwargs": additional_kwargs,
                    }
                )
            generations.append(
                ChatGeneration(
                    message=message,
                    generation_info=generation.generation_info,
                )
            )
        return ChatResult(generations=generations, llm_output=result.llm_output)

    async def build_stream(
        self,
        scenario: str,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """构造本地 fixture 流，用于 provider 格式自检。"""
        part_state = _StreamPartState()
        if scenario == "reasoning_only":
            for delta in ["先", "思考", "一下", "结论"]:
                message = self._delta_to_message_chunks(
                    {"reasoning_content": delta},
                    part_state=part_state,
                )[0]
                yield ChatGenerationChunk(message=message)
            return
        if scenario == "text_only":
            for delta in ["你好", "，", "世界"]:
                message = self._delta_to_message_chunks(
                    {"content": delta},
                    part_state=part_state,
                )[0]
                yield ChatGenerationChunk(message=message)
            return
        if scenario == "mixed_reasoning_text":
            for delta in ["思考", "中"]:
                message = self._delta_to_message_chunks(
                    {"reasoning_content": delta},
                    part_state=part_state,
                )[0]
                yield ChatGenerationChunk(message=message)
            for delta in ["最终", "回答"]:
                message = self._delta_to_message_chunks(
                    {"content": delta},
                    part_state=part_state,
                )[0]
                yield ChatGenerationChunk(message=message)
            return
        if scenario == "tool_call":
            for message in self._delta_to_message_chunks(
                {
                    "content": "调用工具",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_abc",
                            "function": {
                                "name": "list_files",
                                "arguments": '{"path": "."}',
                            },
                        }
                    ],
                },
                part_state=part_state,
            ):
                yield ChatGenerationChunk(message=message)
            return
        if scenario == "reasoning_then_tool":
            for message in self._delta_to_message_chunks(
                {"reasoning_content": "思考"},
                part_state=part_state,
            ):
                yield ChatGenerationChunk(message=message)
            for message in self._delta_to_message_chunks(
                {
                    "content": "决定调用",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_xyz",
                            "function": {
                                "name": "shell",
                                "arguments": '{"cmd": "ls"}',
                            },
                        }
                    ],
                },
                part_state=part_state,
            ):
                yield ChatGenerationChunk(message=message)
            return
        raise ValueError(f"未知 provider 自检场景: {scenario!r}")

    def self_check(self) -> FormatCheckResult:
        import asyncio

        result = asyncio.run(validate_provider_format(self))
        history_sample = [
            AIMessage(
                content=[
                    {
                        "type": "reasoning",
                        "id": "rs_history",
                        "summary": [{"type": "summary_text", "text": "历史推理"}],
                    },
                    {"type": "text", "text": "历史回答"},
                ],
                response_metadata={"model_provider": "openai"},
            )
        ]
        history_check = check_history_messages_accepted(self, history_sample)
        result.add(
            FormatCheckItem(
                name="[history_roundtrip] 历史 reasoning block 可转为 Chat Completions",
                passed=history_check.passed,
                detail=history_check.detail,
                remediation=history_check.remediation,
            )
        )
        return result


def build_litellm_chat_model(
    *,
    provider: dict[str, Any],
    runtime_config: dict[str, Any],
    request_options: dict[str, Any],
    prompt_cache_key: str | None = None,
) -> BoxteamLiteLLMChatModel:
    model_name = provider["model"]

    request_parameters: dict[str, Any] = {}
    runtime_parameter_names = {
        "temperature": "temperature",
        "top_p": "top_p",
        "max_output_tokens": "max_tokens",
    }
    for runtime_name, request_name in runtime_parameter_names.items():
        if runtime_name in runtime_config:
            request_parameters[request_name] = runtime_config[runtime_name]
    request_parameters.update(request_options.get("overrides") or {})
    api_mode = parse_provider_api_mode(provider)
    capabilities = parse_provider_capabilities(provider)
    if prompt_cache_key is not None and PROMPT_CACHE_KEY in capabilities:
        extra_body = request_parameters.get("extra_body") or {}
        if not isinstance(extra_body, dict):
            raise TypeError(
                "Chat Completions request_options.overrides.extra_body 必须是对象"
            )
        request_parameters["extra_body"] = {
            **extra_body,
            "prompt_cache_key": prompt_cache_key,
        }

    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": provider["api_key"],
        "custom_llm_provider": provider["custom_llm_provider"],
        "max_retries": 3,
        "streaming": True,
        "model_kwargs": request_parameters,
        "provider_id": provider.get("id"),
        "reasoning_content_replay": api_mode.supports_reasoning.reasoning_content,
        "thinking_blocks_replay": api_mode.supports_reasoning.thinking_blocks,
        "image_input_replay": "image_input" in capabilities,
        "no_proxy": bool(request_options.get("no_proxy")),
    }

    if provider.get("endpoint"):
        kwargs["api_base"] = provider["endpoint"]
    if request_options.get("default_headers"):
        kwargs["extra_headers"] = request_options["default_headers"]
    return BoxteamLiteLLMChatModel(**kwargs)
