"""LiteLLM 内容块的 canonical 保存格式和 provider 投影。"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage

from app.agents.providers.message_content_schema import validate_content_blocks
from app.services.mapping.agent_content_mapper import extract_reasoning_summary

MISSING = object()
_SERVER_OWNED_FIELDS = {
    "id",
    "status",
    "index",
    "session_id",
    "response_id",
    "conversation_id",
}
_GENERATED_PART_PREFIX = "part_"


def _as_block(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): copy.deepcopy(item) for key, item in value.items()}


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    blocks: list[dict[str, Any]] = []
    for value in content:
        if isinstance(value, str):
            if value:
                blocks.append({"type": "text", "text": value})
            continue
        block = _as_block(value)
        if block is not None:
            blocks.append(block)
        elif value is not None:
            blocks.append({"type": "text", "text": str(value)})
    return blocks


def _append_unique(blocks: list[dict[str, Any]], block: dict[str, Any]) -> None:
    if block not in blocks:
        blocks.append(copy.deepcopy(block))


def _append_stream_block(blocks: list[dict[str, Any]], block: dict[str, Any]) -> None:
    """合并相邻 carrier delta，同时避免 Responses added/done 重复写入。"""
    block_type = block.get("type")
    if blocks and block_type == "reasoning_content":
        previous = blocks[-1]
        if previous.get("type") == "reasoning_content":
            previous_text = previous.get("reasoning_content")
            current_text = block.get("reasoning_content")
            if isinstance(previous_text, str) and isinstance(current_text, str):
                previous["reasoning_content"] = previous_text + current_text
                return
    if blocks and block_type == "reasoning_items":
        previous = blocks[-1]
        if previous.get("type") == "reasoning_items":
            previous_items = previous.get("reasoning_items")
            current_items = block.get("reasoning_items")
            if isinstance(previous_items, list) and isinstance(current_items, list):
                for item in current_items:
                    item_id = item.get("id") if isinstance(item, Mapping) else None
                    if isinstance(item_id, str):
                        replaced = False
                        for index, previous_item in enumerate(previous_items):
                            if (
                                isinstance(previous_item, Mapping)
                                and previous_item.get("id") == item_id
                            ):
                                previous_items[index] = copy.deepcopy(item)
                                replaced = True
                                break
                        if replaced:
                            continue
                    if item not in previous_items:
                        previous_items.append(copy.deepcopy(item))
                return
    _append_unique(blocks, block)


def _reasoning_content_block(text: str) -> dict[str, Any]:
    return {
        "type": "reasoning_content",
        "reasoning_content": text,
    }


def _reasoning_items_block(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "reasoning_items",
        "reasoning_items": copy.deepcopy(items),
    }


def build_ai_message_content(
    content: Any,
    *,
    source_provider: str | None = None,
    source_model: str | None = None,
    reasoning_content: Any = MISSING,
    thinking_blocks: Any = MISSING,
    reasoning_items: Any = MISSING,
) -> Any:
    """把 LiteLLM 响应组装成可持久化的有序 AIMessage.content。

    ``source_provider`` 和 ``source_model`` 只用于调用方的响应元数据，不能
    进入 content。两个 LiteLLM 独立字段各自成为同名 carrier block；provider
    已经放进 content 的 thinking/text block 则整体复制。可执行工具调用仍由
    适配器放入 ``AIMessage.tool_calls``。
    """
    del source_provider, source_model
    direct_blocks = _content_blocks(content)
    direct_types = {block.get("type") for block in direct_blocks}
    blocks: list[dict[str, Any]] = []

    if (
        isinstance(reasoning_content, str)
        and reasoning_content
        and "reasoning_content" not in direct_types
    ):
        blocks.append(_reasoning_content_block(reasoning_content))

    if isinstance(thinking_blocks, list) and not (
        {"thinking", "redacted_thinking"} & direct_types
    ):
        for value in thinking_blocks:
            block = _as_block(value)
            if block is not None and block.get("type") in {
                "thinking",
                "redacted_thinking",
            }:
                blocks.append(block)

    if isinstance(reasoning_items, list) and "reasoning_items" not in direct_types:
        items = [
            block
            for value in reasoning_items
            if (block := _as_block(value)) is not None
        ]
        if items:
            blocks.append(_reasoning_items_block(items))

    for block in direct_blocks:
        blocks.append(copy.deepcopy(block))

    if not blocks:
        return ""
    return validate_content_blocks(blocks)


def _direct_blocks(content: Any) -> list[dict[str, Any]]:
    return _content_blocks(content)


def visible_text(content: Any) -> str:
    """从直接 content blocks 提取可见文本，不读取思考块。"""
    parts: list[str] = []
    for block in _direct_blocks(content):
        block_type = block.get("type")
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == "refusal":
            refusal = block.get("refusal")
            if isinstance(refusal, str):
                parts.append(f"[拒绝]{refusal}")
    return "".join(parts)


def _summary_text(value: Any) -> str:
    return extract_reasoning_summary(value).strip()


def _encrypted_row(value: str) -> dict[str, object]:
    return {
        "kind": "encrypted",
        "encrypted_length": len(value),
        "encrypted_hash": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _reasoning_content_text(block: Mapping[str, Any]) -> str:
    direct = block.get("reasoning")
    if isinstance(direct, str):
        return direct.strip()
    content = block.get("content")
    if isinstance(content, list):
        return "".join(
            str(item.get("text"))
            for item in content
            if isinstance(item, Mapping)
            and item.get("type") in {"reasoning_text", "text"}
            and isinstance(item.get("text"), str)
        ).strip()
    return ""


def reasoning_projection_rows(content: Any) -> list[dict[str, object]]:
    """返回带 source 坐标的 reasoning 投影，不返回 encrypted 正文。"""
    rows: list[dict[str, object]] = []

    def append_row(
        *,
        block_index: int,
        item_index: int,
        carrier_type: str,
        item_id: str | None = None,
        reasoning_text: str | None = None,
        summary_text: str | None = None,
        encrypted: str | None = None,
        signature_present: bool = False,
    ) -> None:
        clean_reasoning = reasoning_text.strip() if reasoning_text else ""
        clean_summary = summary_text.strip() if summary_text else ""
        has_encrypted = bool(encrypted)
        if not clean_reasoning and not clean_summary and not has_encrypted:
            return
        kind = (
            "encrypted"
            if has_encrypted and not (clean_reasoning or clean_summary)
            else "reasoning"
            if clean_reasoning
            else "summary"
        )
        row: dict[str, object] = {
            "kind": kind,
            "text": clean_reasoning or clean_summary,
            "reasoning_text": clean_reasoning or None,
            "summary_text": clean_summary or None,
            "content_block_index": block_index,
            "item_index": item_index,
            "carrier_type": carrier_type,
            "signature_present": signature_present,
        }
        if item_id:
            row["item_id"] = item_id
        if has_encrypted and encrypted is not None:
            row["encrypted_length"] = len(encrypted)
            row["encrypted_hash"] = hashlib.sha256(
                encrypted.encode("utf-8")
            ).hexdigest()
        rows.append(row)

    for block_index, block in enumerate(_direct_blocks(content)):
        block_type = block.get("type")
        if block_type == "reasoning_content":
            text = block.get("reasoning_content")
            append_row(
                block_index=block_index,
                item_index=0,
                carrier_type="reasoning_content",
                reasoning_text=text if isinstance(text, str) else None,
            )
            continue
        if block_type == "reasoning_items":
            items = block.get("reasoning_items")
            if isinstance(items, list):
                for item_index, item in enumerate(items):
                    if not isinstance(item, Mapping):
                        continue
                    append_row(
                        block_index=block_index,
                        item_index=item_index,
                        carrier_type="reasoning_items",
                        item_id=(
                            item.get("id")
                            if isinstance(item.get("id"), str)
                            else None
                        ),
                        reasoning_text=_reasoning_content_text(item),
                        summary_text=_summary_text(item.get("summary")),
                        encrypted=(
                            item.get("encrypted_content")
                            if isinstance(item.get("encrypted_content"), str)
                            else None
                        ),
                    )
            continue
        if block_type == "reasoning":
            append_row(
                block_index=block_index,
                item_index=0,
                carrier_type="reasoning_items",
                item_id=block.get("id") if isinstance(block.get("id"), str) else None,
                reasoning_text=_reasoning_content_text(block),
                summary_text=_summary_text(block.get("summary")),
                encrypted=(
                    block.get("encrypted_content")
                    if isinstance(block.get("encrypted_content"), str)
                    else None
                ),
            )
            continue
        if block_type == "thinking":
            text = block.get("thinking") or block.get("text")
            append_row(
                block_index=block_index,
                item_index=0,
                carrier_type="thinking",
                reasoning_text=text if isinstance(text, str) else None,
                signature_present=(
                    isinstance(block.get("signature"), str)
                    and bool(block.get("signature"))
                ),
            )
            continue
        if block_type == "redacted_thinking":
            encrypted = block.get("data")
            append_row(
                block_index=block_index,
                item_index=0,
                carrier_type="redacted_thinking",
                encrypted=encrypted if isinstance(encrypted, str) else None,
            )
    return rows


def _is_generated_part_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_GENERATED_PART_PREFIX)


def _clean_stream_block(block: Mapping[str, Any]) -> dict[str, Any]:
    """去掉流式合并器的临时 part 字段，恢复 provider 原始 item 身份。"""
    result = {str(key): copy.deepcopy(value) for key, value in block.items()}
    extras = result.get("extras")
    provider_part_id = (
        extras.get("provider_part_id")
        if isinstance(extras, Mapping)
        else None
    )
    generated_id = _is_generated_part_id(result.get("id"))
    if generated_id:
        result.pop("id", None)
    if generated_id or provider_part_id is not None:
        result.pop("index", None)
    if isinstance(provider_part_id, str) and result.get("type") == "reasoning":
        result["id"] = provider_part_id
    return result


def _stream_content_blocks(content: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for raw_block in _direct_blocks(content):
        block = _clean_stream_block(raw_block)
        block_type = block.get("type")
        extras = block.get("extras")
        if block_type == "reasoning" and isinstance(extras, Mapping):
            response_item = _as_block(extras.get("response_item"))
            thinking_block = _as_block(extras.get("thinking_block"))
            if response_item is not None:
                _append_stream_block(blocks, _reasoning_items_block([response_item]))
                continue
            if thinking_block is not None:
                _append_unique(blocks, thinking_block)
                continue
        block.pop("extras", None)
        if block_type == "reasoning" and isinstance(block.get("reasoning"), str):
            _append_stream_block(blocks, _reasoning_content_block(block["reasoning"]))
            continue
        if block_type == "reasoning":
            _append_stream_block(blocks, _reasoning_items_block([block]))
            continue
        if isinstance(block_type, str) and block_type not in {
            "tool_call",
            "tool_call_chunk",
        }:
            _append_stream_block(blocks, block)
    return blocks


def _without_server_state(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): copy.deepcopy(value)
        for key, value in item.items()
        if key not in _SERVER_OWNED_FIELDS and key != "extras"
    }


def _source_provider(
    response_metadata: Mapping[str, Any] | None,
) -> str | None:
    if response_metadata is None:
        return None
    provider = response_metadata.get("provider_id")
    return provider if isinstance(provider, str) and provider else None


def _reasoning_item_for_replay(
    block: Mapping[str, Any],
    *,
    can_replay_encrypted: bool,
) -> dict[str, Any]:
    projected = _without_server_state(block)
    encrypted = projected.get("encrypted_content")
    if isinstance(encrypted, str) and not can_replay_encrypted:
        projected.pop("encrypted_content", None)
    return projected


def project_ai_message_content(
    content: Any,
    *,
    target_provider: str | None,
    target_capabilities: set[str] | frozenset[str],
    response_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按目标 provider 能力投影直接保存的 AIMessage content。"""
    blocks = _direct_blocks(content)
    source_provider = _source_provider(response_metadata)
    same_provider = (
        isinstance(source_provider, str)
        and isinstance(target_provider, str)
        and source_provider == target_provider
    )
    can_replay_encrypted = (
        "encrypted_reasoning_replay" in target_capabilities and same_provider
    )

    visible_blocks: list[dict[str, Any]] = []
    reasoning_content: list[str] = []
    thinking_blocks: list[dict[str, Any]] = []
    reasoning_items: list[dict[str, Any]] = []

    def add_reasoning_item(item: Mapping[str, Any]) -> None:
        text = _reasoning_content_text(item)
        summary = _summary_text(item.get("summary"))
        if text:
            reasoning_content.append(text)
        elif summary:
            reasoning_content.append(summary)
        projected_item = _reasoning_item_for_replay(
            item,
            can_replay_encrypted=can_replay_encrypted,
        )
        if "reasoning_items" in target_capabilities and (
            projected_item.get("content")
            or projected_item.get("summary")
            or projected_item.get("encrypted_content")
        ):
            reasoning_items.append(projected_item)

    for block in blocks:
        block_type = block.get("type")
        if block_type in {"text", "output_text", "refusal", "image", "image_url"}:
            visible_blocks.append(_without_server_state(block))
            continue
        if block_type == "reasoning_content":
            text = block.get("reasoning_content")
            if isinstance(text, str) and text:
                reasoning_content.append(text)
            continue
        if block_type == "reasoning_items":
            items = block.get("reasoning_items")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, Mapping):
                        add_reasoning_item(item)
            continue
        if block_type == "reasoning":
            add_reasoning_item(block)
            continue
        if block_type in {"thinking", "redacted_thinking"}:
            if (
                "thinking_blocks" in target_capabilities
                and (block_type != "redacted_thinking" or can_replay_encrypted)
            ):
                thinking_blocks.append(copy.deepcopy(block))
            continue

    if isinstance(content, str):
        visible: Any = content
    elif not visible_blocks:
        visible = ""
    else:
        visible = visible_blocks

    result: dict[str, Any] = {"content": visible}
    if "reasoning_content_replay" in target_capabilities and reasoning_content:
        result["reasoning_content"] = "\n".join(reasoning_content)
    if "thinking_blocks" in target_capabilities and thinking_blocks:
        result["thinking_blocks"] = thinking_blocks
    if "reasoning_items" in target_capabilities and reasoning_items:
        result["reasoning_items"] = reasoning_items
    return result


def canonicalize_ai_message(
    message: AIMessage,
    *,
    source_provider: str | None,
) -> AIMessage:
    """把流式合并结果收敛为有序、可校验 content blocks，供 checkpoint 保存。"""
    del source_provider
    additional_kwargs = dict(message.additional_kwargs or {})
    blocks = _stream_content_blocks(message.content)

    for key in ("reasoning_content", "thinking_blocks", "reasoning_items"):
        additional_kwargs.pop(key, None)

    normalized_content: Any = validate_content_blocks(blocks)
    if not blocks and isinstance(message.content, str):
        normalized_content = message.content
    return message.model_copy(
        update={
            "content": normalized_content,
            "additional_kwargs": additional_kwargs,
        }
    )
