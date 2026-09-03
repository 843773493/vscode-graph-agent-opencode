"""LiteLLM 内容块的 canonical 保存格式和 provider 投影。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage

from app.agents.providers.message_content_schema import validate_content_blocks
from app.core.message_content_projection import (
    reasoning_projection_rows as _canonical_reasoning_projection_rows,
)
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


def project_user_message_content(
    content: Any,
    *,
    target_format: str,
    image_input: bool,
) -> dict[str, Any]:
    """把 canonical HumanMessage content 投影为 LiteLLM 的临时 blocks。

    ``metadata``、preview 状态和其它 canonical-only 字段不会越过请求边界；
    source content 保持不变。不可选的 rich block 失败只影响该 block，manifest
    文本和附件路径仍会发送，并通过 diagnostics 暴露原因。
    """

    if target_format not in {"chat_completions", "responses"}:
        raise ValueError(
            "用户 content 投影只接受 LiteLLM 的 chat_completions 或 responses 形状，"
            f"不支持: {target_format!r}"
        )

    source_blocks = _content_blocks(content)
    projected: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for index, source in enumerate(source_blocks):
        block_type = source.get("type")
        if block_type in {"text", "input_text", "output_text"}:
            text = source.get("text")
            if isinstance(text, str):
                projected.append(
                    {
                        "type": "input_text" if target_format == "responses" else "text",
                        "text": text,
                    }
                )
            continue
        if block_type == "refusal":
            projected.append(
                {
                    key: copy.deepcopy(value)
                    for key, value in source.items()
                    if key != "metadata"
                }
            )
            continue
        if block_type not in {"image_url", "image", "input_image"}:
            if isinstance(block_type, str) and block_type not in {
                "reasoning",
                "thinking",
                "redacted_thinking",
                "reasoning_content",
                "reasoning_items",
            }:
                diagnostics.append(
                    {
                        "block_index": index,
                        "block_type": block_type,
                        "status": "projection_failed",
                        "detail": f"目标 provider 未定义用户 block 类型: {block_type}",
                    }
                )
            continue
        if not image_input:
            diagnostics.append(
                {
                    "block_index": index,
                    "block_type": block_type,
                    "status": "not_sent",
                    "detail": "目标 provider 未声明 image_input 能力",
                }
            )
            continue

        image_url = source.get("image_url")
        if block_type in {"image_url", "input_image"}:
            image_url = (
                image_url.get("url")
                if isinstance(image_url, Mapping)
                else image_url
            )
        if target_format == "responses":
            if not isinstance(image_url, str) or not image_url:
                diagnostics.append(
                    {
                        "block_index": index,
                        "block_type": block_type,
                        "status": "projection_failed",
                        "detail": "Responses image block 缺少非空 image_url",
                    }
                )
                continue
            projected.append({"type": "input_image", "image_url": image_url})
            continue
        if block_type == "image":
            diagnostics.append(
                {
                    "block_index": index,
                    "block_type": block_type,
                    "status": "projection_failed",
                    "detail": (
                        "应用层不接受 provider 原生 image/source block；"
                        "canonical 用户图片必须使用 image_url 形状"
                    ),
                }
            )
            continue
        if not isinstance(image_url, str) or not image_url:
            diagnostics.append(
                {
                    "block_index": index,
                    "block_type": block_type,
                    "status": "projection_failed",
                    "detail": "image block 缺少可发送的 URL",
                }
            )
            continue
        projected.append({"type": "image_url", "image_url": {"url": image_url}})

    return {
        "content": content if isinstance(content, str) else projected or "",
        "diagnostics": diagnostics,
    }


def _summary_text(value: Any) -> str:
    return extract_reasoning_summary(value).strip()


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
    """调用全局 canonical 投影，避免 provider 回放再生成另一套语义。"""
    return _canonical_reasoning_projection_rows(content)


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
    # TODO: 等上游 Responses provider 明确支持 reasoning.content 后再恢复该字段。
    # Responses reasoning 的 content 是输出态字段；CCTQ 等兼容端会拒绝把它
    # 作为历史输入发送（要求 reasoning.content 数组长度为 0）。
    projected.pop("content", None)
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
