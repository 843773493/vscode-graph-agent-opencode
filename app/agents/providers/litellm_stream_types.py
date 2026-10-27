"""LiteLLM stream state 与 provider response 基础工具。"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_core.messages.ai import InputTokenDetails, UsageMetadata

from app.core.identifier import create_prefixed_id


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return copy.deepcopy(dumped)
    return {}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """读取响应 mapping；调用方需要修改时应自行复制。"""
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, Mapping):
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
    total_tokens = (
        int(raw_total) if raw_total is not None else input_tokens + output_tokens
    )
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


@dataclass(slots=True)
class _StreamedResponsePayloadBuilder:
    """边消费消息块边保留日志响应需要的最小字段。"""

    _reasoning_parts: list[str] = field(default_factory=list)
    _text_parts: list[str] = field(default_factory=list)
    _tool_calls: dict[int, dict[str, str]] = field(default_factory=dict)
    _tool_argument_parts: dict[int, list[str]] = field(default_factory=dict)

    def add(self, message: AIMessageChunk) -> None:
        content = getattr(message, "content", "")
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            if isinstance(block, str):
                if block:
                    self._text_parts.append(block)
                continue
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "reasoning_content":
                reasoning = block.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    self._reasoning_parts.append(reasoning)
            elif block_type in {"text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str) and text:
                    self._text_parts.append(text)

        for fallback_index, raw_tool_call in enumerate(
            getattr(message, "tool_call_chunks", []) or []
        ):
            if not isinstance(raw_tool_call, Mapping):
                continue
            provider_index = raw_tool_call.get("index")
            tool_call_index = (
                provider_index if isinstance(provider_index, int) else fallback_index
            )
            call_metadata = self._tool_calls.setdefault(tool_call_index, {})
            argument_parts = self._tool_argument_parts.setdefault(tool_call_index, [])
            for key in ("id", "name"):
                value = raw_tool_call.get(key)
                if isinstance(value, str) and value:
                    call_metadata[key] = value
            arguments = raw_tool_call.get("args")
            if isinstance(arguments, str) and arguments:
                argument_parts.append(arguments)

    def build(self) -> dict[str, object]:
        message_payload: dict[str, object] = {
            "role": "assistant",
            "content": "".join(self._text_parts) or None,
        }
        reasoning = "".join(self._reasoning_parts)
        if reasoning:
            message_payload["reasoning_content"] = reasoning
        if self._tool_calls:
            message_payload["tool_calls"] = [
                {
                    "id": call.get("id"),
                    "type": "function",
                    "function": {
                        "name": call.get("name"),
                        "arguments": "".join(self._tool_argument_parts[call_index]),
                    },
                }
                for call_index, call in sorted(self._tool_calls.items())
            ]
        return {
            "choices": [
                {
                    "index": 0,
                    "message": message_payload,
                }
            ]
        }


def _close_sync_stream(raw_stream: Any) -> None:
    close = getattr(raw_stream, "close", None)
    if close is not None:
        close()


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
class _ResponsesToolCall:
    """同一 output item 的增量与完成值；完成事件只补发尚未投递的后缀。"""

    item_id: str
    call_id: str
    name: str
    arguments: str
    completed: bool = False

    def append(self, delta: str) -> str:
        if self.completed:
            raise RuntimeError(f"Responses function_call 完成后收到参数增量: call_id={self.call_id}")
        self.arguments += delta
        return delta

    def finish(self, arguments: str) -> str:
        if not arguments.startswith(self.arguments) or (self.completed and arguments != self.arguments):
            raise RuntimeError(f"Responses function_call 完成参数与已投递值冲突: call_id={self.call_id}")
        suffix = arguments[len(self.arguments):]
        self.arguments = arguments
        self.completed = True
        return suffix


@dataclass(slots=True)
class _StreamPartState:
    """为单次模型响应分配稳定的 LangChain content part 身份。"""

    _REASONING_ALIAS_TYPES = frozenset({"reasoning_content", "thinking"})

    next_index: int = 0
    active_kind: str | None = None
    active_part_id: str | None = None
    active_index: int | None = None
    active_provider_part_id: str | None = None
    fallback_item_ids: dict[int, str] | None = None
    reasoning_summary_provider_ids: set[str] = field(default_factory=set)
    responses_tool_calls: dict[int, _ResponsesToolCall] = field(default_factory=dict)
    last_reasoning_alias_type: str | None = None
    last_reasoning_alias_text: str | None = None

    def __post_init__(self) -> None:
        self.fallback_item_ids = {}

    def close(self) -> None:
        self.active_kind = None
        self.active_part_id = None
        self.active_index = None
        self.active_provider_part_id = None

    def accept_reasoning_alias(self, block_type: str, text: str) -> bool:
        """过滤两个 carrier 对同一增量的重复表达。

        部分 Chat Completions 兼容服务会把同一段增量同时暴露为
        ``reasoning_content`` 和 ``thinking``。这两个 carrier 在正常情况下
        仍然必须保持独立；只有相邻 carrier 类型不同且正文完全相同时，才
        认定后一个是别名重复。接受后的 block 继续由 ``decorate`` 使用同一
        part 身份合并。
        """
        # TODO: 待 LiteLLM/各 Chat Completions provider 明确 carrier alias 合同后，
        # 用显式 provider 能力替换当前的相邻正文启发式判断。
        if block_type not in self._REASONING_ALIAS_TYPES:
            return True
        is_duplicate = (
            self.last_reasoning_alias_type in self._REASONING_ALIAS_TYPES
            and self.last_reasoning_alias_type != block_type
            and self.last_reasoning_alias_text == text
        )
        if is_duplicate:
            return False
        self.last_reasoning_alias_type = block_type
        self.last_reasoning_alias_text = text
        return True

    def reset_reasoning_alias(self) -> None:
        """在正文或工具边界后结束 carrier 别名去重窗口。"""
        self.last_reasoning_alias_type = None
        self.last_reasoning_alias_text = None

    def item_id(self, index: int) -> str:
        """为缺少 provider ID 的 reasoning item 保留稳定的本地身份。"""
        if self.fallback_item_ids is None:
            self.fallback_item_ids = {}
        item_id = self.fallback_item_ids.get(index)
        if item_id is None:
            item_id = f"reasoning-item:{index}"
            self.fallback_item_ids[index] = item_id
        return item_id

    def decorate(self, block: dict[str, Any]) -> dict[str, Any]:
        block_type = block.get("type")
        if block_type in {
            "reasoning",
            "reasoning_content",
            "reasoning_items",
            "thinking",
            "redacted_thinking",
        } or block_type in {"text", "output_text", "refusal"}:
            pass
        else:
            self.close()
            return block

        provider_part_id = block.get("id")
        extras = block.get("extras")
        if not isinstance(provider_part_id, str) and isinstance(extras, dict):
            raw_provider_part_id = extras.get("id") or extras.get("provider_part_id")
            if isinstance(raw_provider_part_id, str):
                provider_part_id = raw_provider_part_id
        if not isinstance(provider_part_id, str) and block_type == "reasoning_items":
            items = block.get("reasoning_items")
            first_item = items[0] if isinstance(items, list) and items else None
            item_id = first_item.get("id") if isinstance(first_item, dict) else None
            if isinstance(item_id, str):
                provider_part_id = item_id

        provider_changed = (
            isinstance(provider_part_id, str)
            and self.active_provider_part_id is not None
            and provider_part_id != self.active_provider_part_id
        )
        part_changed = self.active_kind != block_type or provider_changed
        if part_changed:
            self.active_kind = block_type
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
        # LangChain 合并同一个 index 的 block 时，会把未知字符串字段拼接起来。
        # provider_part_id 只在 part 首次出现时写入，避免连续 reasoning delta
        # 变成 ``rs_1rs_1``，同时保留最终 canonicalizer 恢复 provider ID 的依据。
        if isinstance(provider_part_id, str) and part_changed:
            decorated_extras = dict(extras) if isinstance(extras, dict) else {}
            decorated_extras.pop("id", None)
            decorated_extras["provider_part_id"] = provider_part_id
            decorated["extras"] = decorated_extras
        decorated["id"] = self.active_part_id
        decorated["index"] = self.active_index
        return decorated
