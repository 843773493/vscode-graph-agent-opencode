from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
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
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_litellm import ChatLiteLLM

from app.agents.providers._format_check import (
    FormatCheckItem,
    FormatCheckResult,
    check_history_messages_accepted,
    validate_provider_format,
)
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


def _create_usage_metadata(usage: Any) -> UsageMetadata:
    input_tokens = int(_usage_value(usage, "prompt_tokens") or 0)
    output_tokens = int(_usage_value(usage, "completion_tokens") or 0)
    raw_total = _usage_value(usage, "total_tokens")
    total_tokens = int(raw_total) if raw_total is not None else input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


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


def _openai_compatible_model_name(model: str) -> str:
    return model if model.startswith("openai/") else f"openai/{model}"


class BoxteamLiteLLMChatModel(ChatLiteLLM):
    """LiteLLM 模型包装层，统一输出 LangChain 标准 content blocks。"""

    provider_interface: str = "litellm"
    provider_id: str | None = None

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
                    normalized.append(block)
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
                item["content"] = self.normalize_history_content(item.get("content"))
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
                if "reasoning_content" in message.additional_kwargs:
                    message_dict["reasoning_content"] = message.additional_kwargs["reasoning_content"]
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
        params = self._client_params
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

    def _delta_to_message_chunks(self, delta: Mapping[str, Any]) -> list[AIMessageChunk]:
        chunks: list[AIMessageChunk] = []
        reasoning = self._delta_reasoning(delta)
        if reasoning:
            chunks.append(
                AIMessageChunk(
                    content=[{"type": "reasoning", "reasoning": reasoning}],
                )
            )

        content = delta.get("content")
        if content:
            chunks.append(
                AIMessageChunk(
                    content=self.normalize_output_content(content),
                )
            )

        raw_tool_calls = delta.get("tool_calls")
        tool_call_chunks = self._delta_tool_call_chunks(raw_tool_calls)
        if tool_call_chunks:
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
        params["stream_options"] = self.stream_options or {"include_usage": True}

        first_chunk_yielded = False
        for raw_chunk in self.completion_with_retry(
            messages=message_dicts,
            run_manager=run_manager,
            **params,
        ):
            for cg_chunk in self._convert_stream_response_chunk(
                raw_chunk,
                first_chunk_yielded=first_chunk_yielded,
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
        params["stream_options"] = self.stream_options or {"include_usage": True}

        first_chunk_yielded = False
        async for raw_chunk in await self.acompletion_with_retry(
            messages=message_dicts,
            run_manager=run_manager,
            **params,
        ):
            for cg_chunk in self._convert_stream_response_chunk(
                raw_chunk,
                first_chunk_yielded=first_chunk_yielded,
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
        for message_chunk in self._delta_to_message_chunks(delta):
            if usage_metadata:
                message_chunk.usage_metadata = usage_metadata
            if not first_chunk_yielded:
                message_chunk.response_metadata = {
                    "model_name": self.model_name or self.model,
                    "model_provider": "litellm",
                    "provider_interface": self.provider_interface,
                }
                first_chunk_yielded = True
            result.append(ChatGenerationChunk(message=message_chunk))
        return result

    def _create_chat_result(self, response: Mapping[str, Any]) -> ChatResult:
        result = super()._create_chat_result(response)
        generations: list[ChatGeneration] = []
        for generation in result.generations:
            message = generation.message
            if isinstance(message, AIMessage):
                message = message.model_copy(
                    update={
                        "content": self.normalize_output_content(message.content),
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
        if scenario == "reasoning_only":
            for delta in ["先", "思考", "一下", "结论"]:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=[{"type": "reasoning", "reasoning": delta}]
                    )
                )
            return
        if scenario == "text_only":
            for delta in ["你好", "，", "世界"]:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content=[{"type": "text", "text": delta}])
                )
            return
        if scenario == "mixed_reasoning_text":
            for delta in ["思考", "中"]:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=[{"type": "reasoning", "reasoning": delta}]
                    )
                )
            for delta in ["最终", "回答"]:
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content=[{"type": "text", "text": delta}])
                )
            return
        if scenario == "tool_call":
            yield ChatGenerationChunk(
                message=AIMessageChunk(content=[{"type": "text", "text": "调用工具"}])
            )
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "list_files",
                            "args": '{"path": "."}',
                            "id": "call_abc",
                            "index": 0,
                        }
                    ],
                )
            )
            return
        if scenario == "reasoning_then_tool":
            yield ChatGenerationChunk(
                message=AIMessageChunk(content=[{"type": "reasoning", "reasoning": "思考"}])
            )
            yield ChatGenerationChunk(
                message=AIMessageChunk(content=[{"type": "text", "text": "决定调用"}])
            )
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "shell",
                            "args": '{"cmd": "ls"}',
                            "id": "call_xyz",
                            "index": 0,
                        }
                    ],
                )
            )
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
    openai_compatible: bool,
    request_options: dict[str, Any],
) -> BoxteamLiteLLMChatModel:
    model_name = provider["model"]
    if openai_compatible:
        model_name = _openai_compatible_model_name(model_name)

    model_kwargs: dict[str, Any] = {}
    if request_options.get("extra_body"):
        model_kwargs["extra_body"] = request_options["extra_body"]

    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": provider["api_key"],
        "temperature": runtime_config["temperature"],
        "top_p": runtime_config["top_p"],
        "max_tokens": runtime_config["max_output_tokens"],
        "max_retries": 3,
        "streaming": True,
        "model_kwargs": model_kwargs,
        "provider_interface": provider.get("interface") or "litellm",
        "provider_id": provider.get("id"),
    }
    if provider.get("endpoint"):
        kwargs["api_base"] = provider["endpoint"]
    if request_options.get("default_headers"):
        kwargs["extra_headers"] = request_options["default_headers"]
    return BoxteamLiteLLMChatModel(**kwargs)
