"""provider request 的无 I/O content 投影。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from app.services.mapping.itemized.content_blocks import direct_content_blocks

_SERVER_OWNED_FIELDS = {
    "id",
    "status",
    "index",
    "session_id",
    "response_id",
    "conversation_id",
}


def project_user_message_content(
    content: Any,
    *,
    target_format: str,
    image_input: bool,
) -> dict[str, Any]:
    """把 canonical HumanMessage content 投影为目标 provider blocks。"""
    if target_format not in {"chat_completions", "responses"}:
        raise ValueError(
            "用户 content 投影只接受 LiteLLM 的 chat_completions 或 responses 形状，"
            f"不支持: {target_format!r}"
        )

    source_blocks = direct_content_blocks(content)
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
                image_url.get("url") if isinstance(image_url, Mapping) else image_url
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
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(
            str(item.get("text"))
            for item in value
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        ).strip()
    return ""


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


def _without_server_state(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): copy.deepcopy(value)
        for key, value in item.items()
        if key not in _SERVER_OWNED_FIELDS and key != "extras"
    }


def _source_provider(response_metadata: Mapping[str, Any] | None) -> str | None:
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
    """按目标 provider 能力投影已规范化的 AIMessage content。"""
    blocks = direct_content_blocks(content)
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


__all__ = ["project_ai_message_content", "project_user_message_content"]
