"""LiteLLM stream state 与 provider response 基础工具。"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
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


def _streamed_response_payload(
    chunks: Sequence[AIMessageChunk],
) -> dict[str, object]:
    """从已解析的 Chat SDK chunks 构造可审查的 upstream response 摘要。"""

    reasoning_parts: list[str] = []
    text_parts: list[str] = []
    tool_calls: dict[int, dict[str, str]] = {}
    for message in chunks:
        content = getattr(message, "content", "")
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "reasoning_content":
                reasoning = block.get("reasoning_content")
                if isinstance(reasoning, str):
                    reasoning_parts.append(reasoning)
            elif block_type in {"text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)

        for fallback_index, raw_tool_call in enumerate(
            getattr(message, "tool_call_chunks", []) or []
        ):
            if not isinstance(raw_tool_call, Mapping):
                continue
            raw_index = raw_tool_call.get("index")
            index = raw_index if isinstance(raw_index, int) else fallback_index
            current = tool_calls.setdefault(index, {})
            for key in ("id", "name"):
                value = raw_tool_call.get(key)
                if isinstance(value, str) and value:
                    current[key] = value
            arguments = raw_tool_call.get("args")
            if isinstance(arguments, str):
                current["arguments"] = current.get("arguments", "") + arguments

    message_payload: dict[str, object] = {
        "role": "assistant",
        "content": "".join(text_parts) or None,
    }
    reasoning = "".join(reasoning_parts)
    if reasoning:
        message_payload["reasoning_content"] = reasoning
    if tool_calls:
        message_payload["tool_calls"] = [
            {
                "id": call.get("id"),
                "type": "function",
                "function": {
                    "name": call.get("name"),
                    "arguments": call.get("arguments", ""),
                },
            }
            for _index, call in sorted(tool_calls.items())
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
class _StreamPartState:
    """为单次模型响应分配稳定的 LangChain content part 身份。"""

    next_index: int = 0
    active_kind: str | None = None
    active_part_id: str | None = None
    active_index: int | None = None
    active_provider_part_id: str | None = None
    fallback_item_ids: dict[int, str] | None = None
    reasoning_summary_provider_ids: set[str] = field(default_factory=set)
    responses_tool_call_ids_by_item_id: dict[str, str] = field(default_factory=dict)
    responses_tool_call_ids_by_output_index: dict[int, str] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        self.fallback_item_ids = {}

    def close(self) -> None:
        self.active_kind = None
        self.active_part_id = None
        self.active_index = None
        self.active_provider_part_id = None

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
