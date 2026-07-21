from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ChatMessage,
    FunctionMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import InputTokenDetails, UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_litellm import ChatLiteLLM

from app.agents.provider_capabilities import (
    PROMPT_CACHE_KEY,
    REASONING_CONTENT_REPLAY,
    parse_provider_capabilities,
)
from app.agents.providers._format_check import (
    FormatCheckItem,
    FormatCheckResult,
    check_history_messages_accepted,
    validate_provider_format,
)
from app.agents.upstream_request_trace import attach_upstream_trace_callback
from app.core.identifier import create_prefixed_id
from app.services.mapping.agent_content_mapper import extract_reasoning_summary


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    return {}


def _usage_value(usage: Any, key: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def _first_usage_value(usage: Any, *keys: str) -> Any:
    for key in keys:
        value = _usage_value(usage, key)
        if value is not None:
            return value
    return None


def _create_usage_metadata(usage: Any) -> UsageMetadata:
    input_tokens = int(_usage_value(usage, "prompt_tokens") or 0)
    output_tokens = int(_usage_value(usage, "completion_tokens") or 0)
    raw_total = _usage_value(usage, "total_tokens")
    total_tokens = int(raw_total) if raw_total is not None else input_tokens + output_tokens
    metadata: UsageMetadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    prompt_details = _usage_value(usage, "prompt_tokens_details")
    cached_tokens = _first_usage_value(
        prompt_details,
        "cached_tokens",
    )
    if cached_tokens is None:
        cached_tokens = _first_usage_value(
            usage,
            "cache_read_input_tokens",
            "prompt_cache_hit_tokens",
        )
    cache_creation_tokens = _first_usage_value(
        prompt_details,
        "cache_creation_tokens",
        "cache_write_tokens",
    )
    if cache_creation_tokens is None:
        cache_creation_tokens = _usage_value(usage, "cache_creation_input_tokens")

    input_details: InputTokenDetails = {}
    if cached_tokens is not None:
        input_details["cache_read"] = int(cached_tokens)
    if cache_creation_tokens is not None:
        input_details["cache_creation"] = int(cache_creation_tokens)
    if input_details:
        metadata["input_token_details"] = input_details
    return metadata


def _message_chunk_token(message: AIMessageChunk) -> str:
    text_attr = getattr(message, "text", "")
    if isinstance(text_attr, str):
        return text_attr
    if callable(text_attr):
        return text_attr()
    return ""


def _openai_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "id": tool_call["id"],
        "function": {
            "name": tool_call["name"],
            "arguments": json.dumps(tool_call.get("args") or {}, ensure_ascii=False),
        },
    }


@dataclass(slots=True)
class _StreamPartState:
    """为单次模型响应分配稳定的 LangChain content part 身份。"""

    next_index: int = 0
    active_kind: str | None = None
    active_part_id: str | None = None
    active_index: int | None = None
    active_provider_part_id: str | None = None

    def close(self) -> None:
        self.active_kind = None
        self.active_part_id = None
        self.active_index = None
        self.active_provider_part_id = None

    def decorate(self, block: dict[str, Any]) -> dict[str, Any]:
        block_type = block.get("type")
        if block_type == "reasoning":
            kind = "reasoning"
        elif block_type in {"text", "output_text", "refusal"}:
            kind = "markdown"
        else:
            self.close()
            return block

        provider_part_id = block.get("id")
        extras = block.get("extras")
        if not isinstance(provider_part_id, str) and isinstance(extras, dict):
            raw_provider_part_id = extras.get("id") or extras.get("provider_part_id")
            if isinstance(raw_provider_part_id, str):
                provider_part_id = raw_provider_part_id

        provider_changed = (
            isinstance(provider_part_id, str)
            and self.active_provider_part_id is not None
            and provider_part_id != self.active_provider_part_id
        )
        if self.active_kind != kind or provider_changed:
            self.active_kind = kind
            self.active_part_id = create_prefixed_id("part")
            self.active_index = self.next_index
            self.active_provider_part_id = (
                provider_part_id if isinstance(provider_part_id, str) else None
            )
            self.next_index += 1
        elif isinstance(provider_part_id, str) and self.active_provider_part_id is None:
            self.active_provider_part_id = provider_part_id

        if self.active_part_id is None or self.active_index is None:
            raise RuntimeError("模型流 content part 状态未初始化")

        decorated = dict(block)
        if isinstance(provider_part_id, str):
            decorated_extras = dict(extras) if isinstance(extras, dict) else {}
            decorated_extras.pop("id", None)
            decorated_extras["provider_part_id"] = provider_part_id
            decorated["extras"] = decorated_extras
        decorated["id"] = self.active_part_id
        decorated["index"] = self.active_index
        return decorated


class BoxteamLiteLLMChatModel(ChatLiteLLM):
    """LiteLLM 模型包装层，统一输出 LangChain 标准 content blocks。"""

    provider_id: str | None = None
    reasoning_content_replay: bool = False

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
            "所有半截内容均已丢弃，未提交工具调用。"
        )

    def _collect_complete_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        run_manager: CallbackManagerForLLMRun | None,
        params: dict[str, Any],
    ) -> list[Any]:
        attempts = self._stream_attempt_count()
        for _attempt in range(1, attempts + 1):
            raw_stream = self.completion_with_retry(
                messages=messages,
                run_manager=run_manager,
                **params,
            )
            raw_chunks = list(raw_stream)
            if self._has_real_stream_termination(raw_stream):
                return raw_chunks
        raise self._incomplete_stream_error(attempts)

    async def _collect_complete_astream(
        self,
        *,
        messages: list[dict[str, Any]],
        run_manager: AsyncCallbackManagerForLLMRun | None,
        params: dict[str, Any],
    ) -> list[Any]:
        attempts = self._stream_attempt_count()
        for _attempt in range(1, attempts + 1):
            raw_stream = await self.acompletion_with_retry(
                messages=messages,
                run_manager=run_manager,
                **params,
            )
            raw_chunks = [raw_chunk async for raw_chunk in raw_stream]
            if self._has_real_stream_termination(raw_stream):
                return raw_chunks
        raise self._incomplete_stream_error(attempts)

    @staticmethod
    def normalize_history_content(content: Any) -> Any:
        """把 checkpoint 历史消息转换为 LiteLLM/OpenAI-compatible 可接受内容。"""
        if not isinstance(content, list):
            return content

        normalized: list[dict[str, Any] | Any] = []
        changed = False
        for block in content:
            if isinstance(block, str):
                normalized.append({"type": "text", "text": block})
                changed = True
                continue
            if not isinstance(block, dict):
                normalized.append({"type": "text", "text": str(block)})
                changed = True
                continue

            block_type = block.get("type")
            if block_type in {"reasoning", "thinking", "redacted_thinking"}:
                changed = True
                continue
            if block_type == "output_text":
                text = block.get("text")
                if isinstance(text, str):
                    normalized.append({"type": "text", "text": text})
                changed = True
                continue
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str):
                    normalized.append({"type": "text", "text": text})
                    if set(block) != {"type", "text"}:
                        changed = True
                else:
                    changed = True
                continue

            fallback_text = block.get("text")
            if isinstance(fallback_text, str):
                normalized.append({"type": "text", "text": fallback_text})
                changed = True
                continue
            normalized.append(block)

        if not normalized:
            return ""
        if not changed:
            return content
        return normalized

    @staticmethod
    def normalize_output_content(content: Any) -> Any:
        """把 LiteLLM 输出中的 thinking/output_text 等方言转为标准块。"""
        if content is None:
            return ""
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else ""
        if not isinstance(content, list):
            return [{"type": "text", "text": str(content)}]

        normalized: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, str):
                if block:
                    normalized.append({"type": "text", "text": block})
                continue
            if not isinstance(block, dict):
                normalized.append({"type": "text", "text": str(block)})
                continue

            block_type = block.get("type")
            if block_type == "reasoning":
                reasoning = block.get("reasoning")
                if not isinstance(reasoning, str):
                    reasoning = extract_reasoning_summary(block.get("summary"))
                reasoning_block: dict[str, Any] = {
                    "type": "reasoning",
                    "reasoning": reasoning,
                }
                extras = dict(block.get("extras") or {})
                if isinstance(block.get("id"), str):
                    extras["id"] = block["id"]
                if extras:
                    reasoning_block["extras"] = extras
                normalized.append(reasoning_block)
                continue
            if block_type in {"thinking", "redacted_thinking"}:
                thinking = block.get("thinking") or block.get("text") or ""
                reasoning_block = {
                    "type": "reasoning",
                    "reasoning": str(thinking),
                }
                extras = {
                    key: value
                    for key, value in block.items()
                    if key not in {"type", "thinking", "text"}
                }
                if extras:
                    reasoning_block["extras"] = extras
                normalized.append(reasoning_block)
                continue
            if block_type in {"text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str):
                    normalized.append({"type": "text", "text": text})
                continue
            if block_type in {"tool_call", "tool_call_chunk", "refusal"}:
                normalized.append(dict(block))
                continue

            fallback_text = block.get("text")
            if isinstance(fallback_text, str):
                normalized.append({"type": "text", "text": fallback_text})

        return normalized or ""

    def _normalize_history_content(self, content: Any) -> Any:
        return self.normalize_history_content(content)

    @staticmethod
    def _history_reasoning_content(content: Any) -> str | None:
        """从 LangChain 标准 content blocks 提取可回放的思考文本。"""
        if not isinstance(content, list):
            return None

        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "reasoning":
                reasoning = block.get("reasoning")
                if not isinstance(reasoning, str):
                    reasoning = extract_reasoning_summary(block.get("summary"))
            elif block_type in {"thinking", "redacted_thinking"}:
                reasoning = block.get("thinking") or block.get("text")
            else:
                continue
            if isinstance(reasoning, str) and reasoning:
                parts.append(reasoning)
        return "\n".join(parts) or None

    def _apply_reasoning_content_replay(
        self,
        message_dict: dict[str, Any],
        *,
        content: Any,
        explicit_reasoning: Any = None,
    ) -> None:
        if not self.reasoning_content_replay:
            message_dict.pop("reasoning_content", None)
            return
        reasoning = (
            explicit_reasoning
            if isinstance(explicit_reasoning, str) and explicit_reasoning
            else self._history_reasoning_content(content)
        )
        if reasoning:
            message_dict["reasoning_content"] = reasoning

    def _convert_messages_to_dicts(self, messages: Sequence[BaseMessage | dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, dict):
                item = dict(message)
                role = item.get("role")
                if role == "human":
                    item["role"] = "user"
                elif role == "ai":
                    item["role"] = "assistant"
                original_content = item.get("content")
                item["content"] = self.normalize_history_content(original_content)
                if item.get("role") == "assistant":
                    self._apply_reasoning_content_replay(
                        item,
                        content=original_content,
                        explicit_reasoning=item.get("reasoning_content"),
                    )
                result.append(item)
                continue

            message_dict: dict[str, Any] = {
                "content": self.normalize_history_content(message.content),
            }
            if isinstance(message, ChatMessage):
                message_dict["role"] = message.role
            elif isinstance(message, HumanMessage):
                message_dict["role"] = "user"
            elif isinstance(message, AIMessage):
                message_dict["role"] = "assistant"
                if message.tool_calls:
                    message_dict["tool_calls"] = [
                        _openai_tool_call(tool_call)
                        for tool_call in message.tool_calls
                    ]
                elif "tool_calls" in message.additional_kwargs:
                    message_dict["tool_calls"] = message.additional_kwargs["tool_calls"]
                if "function_call" in message.additional_kwargs:
                    message_dict["function_call"] = message.additional_kwargs["function_call"]
                self._apply_reasoning_content_replay(
                    message_dict,
                    content=message.content,
                    explicit_reasoning=message.additional_kwargs.get("reasoning_content"),
                )
            elif isinstance(message, SystemMessage):
                message_dict["role"] = "system"
            elif isinstance(message, FunctionMessage):
                message_dict["role"] = "function"
                message_dict["name"] = message.name
            elif isinstance(message, ToolMessage):
                message_dict["role"] = "tool"
                message_dict["tool_call_id"] = message.tool_call_id
                if message.name:
                    message_dict["name"] = message.name
            else:
                raise ValueError(f"未知 LangChain message 类型: {type(message).__name__}")

            if message.name and "name" not in message_dict:
                message_dict["name"] = message.name
            result.append(message_dict)
        return result

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
        if reasoning:
            chunks.append(
                AIMessageChunk(
                    content=self._stream_content(
                        [{"type": "reasoning", "reasoning": reasoning}],
                        part_state=part_state,
                    ),
                )
            )

        content = delta.get("content")
        if content:
            chunks.append(
                AIMessageChunk(
                    content=self._stream_content(content, part_state=part_state),
                )
            )

        raw_tool_calls = delta.get("tool_calls")
        tool_call_chunks = self._delta_tool_call_chunks(raw_tool_calls)
        if tool_call_chunks:
            part_state.close()
            chunks.append(
                AIMessageChunk(
                    content="",
                    additional_kwargs={"tool_calls": raw_tool_calls},
                    tool_call_chunks=tool_call_chunks,  # type: ignore[arg-type]
                )
            )

        provider_specific_fields = (
            delta.get("provider_specific_fields")
            or delta.get("vertex_ai_grounding_metadata")
        )
        if provider_specific_fields is not None:
            chunks.append(
                AIMessageChunk(
                    content="",
                    additional_kwargs={"provider_specific_fields": provider_specific_fields},
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

        first_chunk_yielded = False
        part_state = _StreamPartState()
        raw_chunks = self._collect_complete_stream(
            messages=message_dicts,
            run_manager=run_manager,
            params=params,
        )
        for raw_chunk in raw_chunks:
            for cg_chunk in self._convert_stream_response_chunk(
                raw_chunk,
                first_chunk_yielded=first_chunk_yielded,
                part_state=part_state,
            ):
                first_chunk_yielded = True
                if run_manager:
                    run_manager.on_llm_new_token(
                        _message_chunk_token(cg_chunk.message),
                        chunk=cg_chunk,
                    )
                yield cg_chunk

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

        first_chunk_yielded = False
        part_state = _StreamPartState()
        raw_chunks = await self._collect_complete_astream(
            messages=message_dicts,
            run_manager=run_manager,
            params=params,
        )
        for raw_chunk in raw_chunks:
            for cg_chunk in self._convert_stream_response_chunk(
                raw_chunk,
                first_chunk_yielded=first_chunk_yielded,
                part_state=part_state,
            ):
                first_chunk_yielded = True
                if run_manager:
                    await run_manager.on_llm_new_token(
                        _message_chunk_token(cg_chunk.message),
                        chunk=cg_chunk,
                    )
                yield cg_chunk

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
            delta["vertex_ai_grounding_metadata"] = chunk["vertex_ai_grounding_metadata"]

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

    def _create_chat_result(self, response: Mapping[str, Any]) -> ChatResult:
        result = super()._create_chat_result(response)
        generations: list[ChatGeneration] = []
        for generation in result.generations:
            message = generation.message
            if isinstance(message, AIMessage):
                part_state = _StreamPartState()
                message = message.model_copy(
                    update={
                        "content": self._stream_content(
                            message.content,
                            part_state=part_state,
                        ),
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
    capabilities = parse_provider_capabilities(provider)
    if prompt_cache_key is not None and PROMPT_CACHE_KEY in capabilities:
        extra_body = request_parameters.get("extra_body") or {}
        if not isinstance(extra_body, dict):
            raise TypeError("Chat Completions request_options.overrides.extra_body 必须是对象")
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
        "reasoning_content_replay": REASONING_CONTENT_REPLAY in capabilities,
    }

    if provider.get("endpoint"):
        kwargs["api_base"] = provider["endpoint"]
    if request_options.get("default_headers"):
        kwargs["extra_headers"] = request_options["default_headers"]
    return BoxteamLiteLLMChatModel(**kwargs)
