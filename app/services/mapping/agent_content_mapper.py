from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from langchain_core.utils._merge import merge_dicts

from app.schemas.event import ModelTokenUsagePayload

_TEXT_CHUNKS_KEY = "_stream_text_chunks"

STREAM_TEXT_FIELD_BY_BLOCK_TYPE = {
    "output_text": "text",
    "text": "text",
    "reasoning_content": "reasoning_content",
    "thinking": "thinking",
    "reasoning": "reasoning",
    "refusal": "refusal",
}


@dataclass(frozen=True, slots=True)
class AgentStreamContentPart:
    part_id: str
    index: int
    kind: Literal["reasoning", "markdown", "content"]
    block_type: str
    text: str
    # 除身份和已提取文本外的完整 provider 字段；不承担业务状态判定。
    provider_fields: dict[str, object]


def extract_reasoning_summary(summary: Any) -> str:
    if summary is None:
        return ""
    if isinstance(summary, str):
        return summary
    if not isinstance(summary, list):
        return str(summary)

    parts: list[str] = []
    for entry in summary:
        if isinstance(entry, str):
            parts.append(entry)
            continue
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _reasoning_block_text(block_type: object, block: dict[str, Any]) -> str:
    if block_type == "reasoning_content":
        value = block.get("reasoning_content")
        return value if isinstance(value, str) else ""
    if block_type == "reasoning_items":
        items = block.get("reasoning_items")
        if not isinstance(items, list):
            return ""
        return "".join(
            _reasoning_block_text("reasoning", item)
            for item in items
            if isinstance(item, dict)
        )
    if block_type == "redacted_thinking":
        return ""
    if block_type == "thinking":
        value = block.get("thinking") or block.get("text")
        return value if isinstance(value, str) else ""
    if block_type == "reasoning":
        value = block.get("reasoning")
        if isinstance(value, str) and value:
            return value
        raw_content = block.get("content")
        if isinstance(raw_content, list):
            value = "".join(
                item.get("text", "")
                for item in raw_content
                if isinstance(item, dict)
                and item.get("type") in {"reasoning_text", "text"}
                and isinstance(item.get("text"), str)
            )
            if value:
                return value
        return extract_reasoning_summary(block.get("summary"))
    return ""


def split_agent_content(content: Any) -> tuple[str, str]:
    """把 LangChain content blocks 拆成 reasoning 文本和用户可见文本。"""
    if content is None:
        return "", ""
    if isinstance(content, str):
        return "", content
    if not isinstance(content, list):
        text = str(content)
        return "", text

    reasoning_parts: list[str] = []
    text_parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            text_parts.append(str(part))
            continue

        part_type = part.get("type")
        if part_type in {
            "reasoning",
            "reasoning_content",
            "reasoning_items",
            "thinking",
            "redacted_thinking",
        }:
            reasoning_parts.append(_reasoning_block_text(part_type, part))
            continue

        if part_type in ("text", "output_text"):
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
            continue

        if part_type == "refusal":
            refusal = part.get("refusal")
            if isinstance(refusal, str):
                text_parts.append(f"[拒绝]{refusal}")
            continue

        fallback = part.get("text")
        if isinstance(fallback, str):
            text_parts.append(fallback)

    reasoning_text = "".join(reasoning_parts)
    visible_text = "".join(text_parts)
    return reasoning_text, visible_text


def extract_agent_stream_content_parts(content: Any) -> list[AgentStreamContentPart]:
    """读取带权威身份的模型流 content blocks。"""
    if content in (None, ""):
        return []
    if not isinstance(content, list):
        raise TypeError(
            "模型流 content 必须是带 id/index 的 LangChain content blocks，"
            f"实际类型: {type(content).__name__}"
        )

    result: list[AgentStreamContentPart] = []
    for position, block in enumerate(content):
        if not isinstance(block, dict):
            raise TypeError(
                f"模型流 content[{position}] 必须是 dict，实际类型: {type(block).__name__}"
            )
        block_type = block.get("type")
        if block_type in {"tool_call", "tool_call_chunk"}:
            continue
        if block_type in {
            "reasoning",
            "reasoning_content",
            "reasoning_items",
            "thinking",
            "redacted_thinking",
        }:
            text = _reasoning_block_text(block_type, block)
            kind: Literal["reasoning", "markdown", "content"] = "reasoning"
        elif block_type in {"text", "output_text"}:
            text = block.get("text")
            kind = "markdown"
        elif block_type == "refusal":
            text = block.get("refusal")
            kind = "markdown"
        else:
            if not isinstance(block_type, str) or not block_type:
                raise ValueError(f"模型流 content[{position}] 缺少有效 block type")
            # 已接受的媒体与 Provider 扩展保持结构，不伪装成空文本。
            text = ""
            kind = "content"

        if not isinstance(text, str):
            raise TypeError(
                f"模型流 content[{position}] 的文本字段必须是 str，实际值: {text!r}"
            )

        part_id = block.get("id")
        if not isinstance(part_id, str) or not part_id:
            raise ValueError(f"模型流 content[{position}] 缺少权威 part id")
        index = block.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError(
                f"模型流 content[{position}] 缺少有效 block index: {index!r}"
            )
        result.append(
            AgentStreamContentPart(
                part_id=part_id,
                index=index,
                kind=kind,
                block_type=block_type,
                text=text,
                provider_fields={
                    key: copy.deepcopy(value)
                    for key, value in block.items()
                    if key not in {
                        "type", "id", "index",
                        STREAM_TEXT_FIELD_BY_BLOCK_TYPE.get(block_type),
                    }
                },
            )
        )
    return result


def merge_model_content_part(
    part: AgentStreamContentPart,
    existing: dict[str, object] | None,
) -> dict[str, object]:
    """把规范化 model block/delta 合并到本次请求的有序内存视图。"""
    text_key = STREAM_TEXT_FIELD_BY_BLOCK_TYPE.get(part.block_type)
    if existing is not None and (
        existing.get("type") != part.block_type or existing.get("index") != part.index
    ):
        raise RuntimeError(
            f"模型流 part 身份冲突: part_id={part.part_id} "
            f"existing={existing!r} incoming={part!r}"
        )
    if existing is None or part.kind == "content":
        initial = copy.deepcopy(part.provider_fields)
        initial.update({
            "type": part.block_type,
            "id": part.part_id,
            "index": part.index,
        })
        if text_key is not None:
            initial[text_key] = part.text
            initial[_TEXT_CHUNKS_KEY] = [part.text]
        return initial
    if part.block_type == "reasoning_items":
        current_items = existing.get("reasoning_items")
        incoming_items = part.provider_fields.get("reasoning_items")
        if not isinstance(current_items, list) or not isinstance(incoming_items, list):
            raise TypeError(f"模型流 part 缺少 reasoning_items: part_id={part.part_id}")
        current_item_indexes: dict[str, int] = {}
        for item_position, current in enumerate(current_items):
            current_id = current.get("id") if isinstance(current, dict) else None
            if isinstance(current_id, str):
                current_item_indexes.setdefault(current_id, item_position)
        for incoming in incoming_items:
            incoming_id = incoming.get("id") if isinstance(incoming, dict) else None
            if isinstance(incoming_id, str):
                item_position = current_item_indexes.get(incoming_id)
                if item_position is not None:
                    current_items[item_position] = copy.deepcopy(incoming)
                else:
                    item_position = len(current_items)
                    current_items.append(copy.deepcopy(incoming))
                    current_item_indexes[incoming_id] = item_position
            elif incoming not in current_items:
                current_items.append(copy.deepcopy(incoming))
    elif part.block_type == "redacted_thinking":
        data = part.provider_fields.get("data")
        if isinstance(data, str):
            existing["data"] = data
    elif text_key is not None:
        text_chunks = existing.get(_TEXT_CHUNKS_KEY)
        if not isinstance(text_chunks, list):
            raise TypeError(f"模型流 part 缺少 {_TEXT_CHUNKS_KEY}: part_id={part.part_id}")
        text_chunks.append(part.text)
    provider_fields = copy.deepcopy(part.provider_fields)
    provider_fields.pop("reasoning_items", None)
    if part.block_type == "redacted_thinking":
        provider_fields.pop("data", None)
    extras = provider_fields.pop("extras", None)
    # 正文仍按片段一次拼接；其余字段沿用 LangChain 的签名/扩展增量合并合同。
    existing.update(merge_dicts(existing, provider_fields))
    if isinstance(extras, dict):
        existing_extras = existing.get("extras")
        merged_extras = (
            dict(existing_extras) if isinstance(existing_extras, dict) else {}
        )
        merged_extras.update(extras)
        existing["extras"] = merged_extras

    return existing


def materialize_model_content_part(part: dict[str, object]) -> dict[str, object]:
    """封存一个已累计的来源块；仅在收口时拼接文本。"""
    text_chunks = part.pop(_TEXT_CHUNKS_KEY, None)
    if text_chunks is not None:
        block_type = part["type"]
        part[STREAM_TEXT_FIELD_BY_BLOCK_TYPE[cast(str, block_type)]] = "".join(
            cast(list[str], text_chunks)
        )
    return part


def _usage_token_count(
    usage: Mapping[str, object],
    key: str,
) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"模型 usage_metadata.{key} 必须是非负整数，实际值: {value!r}")
    return value


def stream_chunk_token_usage(chunk: object) -> ModelTokenUsagePayload | None:
    message = getattr(chunk, "message", None)
    usage_metadata = getattr(message or chunk, "usage_metadata", None)
    if usage_metadata is None:
        return None
    if not isinstance(usage_metadata, Mapping):
        raise TypeError(
            "模型 chunk usage_metadata 必须是 mapping，"
            f"实际类型: {type(usage_metadata).__name__}"
        )

    raw_input_details = usage_metadata.get("input_token_details")
    cache_read_input_tokens: int | None = None
    if raw_input_details is not None:
        if not isinstance(raw_input_details, Mapping):
            raise TypeError(
                "模型 usage_metadata.input_token_details 必须是 mapping，"
                f"实际类型: {type(raw_input_details).__name__}"
            )
        if "cache_read" in raw_input_details:
            cache_read_input_tokens = _usage_token_count(
                raw_input_details,
                "cache_read",
            )

    return ModelTokenUsagePayload(
        input_tokens=_usage_token_count(usage_metadata, "input_tokens"),
        output_tokens=_usage_token_count(usage_metadata, "output_tokens"),
        total_tokens=_usage_token_count(usage_metadata, "total_tokens"),
        cache_read_input_tokens=cache_read_input_tokens,
        model_calls=1,
        reported_model_calls=1,
    )
