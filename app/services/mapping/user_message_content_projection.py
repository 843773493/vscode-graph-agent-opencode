"""用户消息 canonical content 的 display 映射。"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    result: list[dict[str, Any]] = []
    for value in content:
        if isinstance(value, str):
            if value:
                result.append({"type": "text", "text": value})
        elif isinstance(value, Mapping):
            result.append(
                {str(key): copy.deepcopy(item) for key, item in value.items()}
            )
        elif value is not None:
            result.append({"type": "text", "text": str(value)})
    return result


@dataclass(frozen=True, slots=True)
class UserContentProjection:
    """用户消息在 display 边界的结构化投影。"""

    blocks: tuple[dict[str, Any], ...]
    visible_text: str
    attachments: tuple[dict[str, Any], ...]
    rich_blocks: tuple[dict[str, Any], ...]
    unknown_block_types: tuple[str, ...]


_VISIBLE_BLOCK_TYPES = {"text", "input_text", "output_text", "refusal"}
_RICH_BLOCK_TYPES = {
    "image",
    "image_url",
    "input_image",
    "document",
    "file",
    "audio",
    "video",
    "input_audio",
    "input_video",
}
_NON_DISPLAY_BLOCK_TYPES = {
    "reasoning",
    "thinking",
    "redacted_thinking",
    "reasoning_content",
    "reasoning_items",
}


def _attachment_metadata(block: Mapping[str, Any]) -> Mapping[str, Any] | None:
    metadata = block.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    if metadata.get("origin") != "generated":
        return None
    if metadata.get("kind") not in {"attachment_manifest", "attachment_preview"}:
        return None
    return metadata


def _metadata_attachment(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    file_id = metadata.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    return {
        "file_id": file_id,
        **{
            key: copy.deepcopy(metadata[key])
            for key in ("name", "content_type", "path", "preview_status")
            if key in metadata
        },
    }


def user_content_projection(
    content: Any,
    response_metadata: Mapping[object, object] | None = None,
) -> UserContentProjection:
    """按 block 提取用户正文、附件和未知 rich block。"""

    blocks = _blocks(content)
    text_parts: list[str] = []
    rich_blocks: list[dict[str, Any]] = []
    unknown_types: list[str] = []
    block_attachments: list[dict[str, Any]] = []

    for block in blocks:
        block_type = block.get("type")
        attachment_metadata = _attachment_metadata(block)
        if attachment_metadata is not None:
            attachment = _metadata_attachment(attachment_metadata)
            if (
                attachment is not None
                and attachment_metadata.get("kind") == "attachment_manifest"
            ):
                block_attachments.append(attachment)

        if block_type in _VISIBLE_BLOCK_TYPES:
            if attachment_metadata is not None:
                continue
            if block_type == "refusal":
                value = block.get("refusal")
                if isinstance(value, str):
                    text_parts.append(f"[拒绝]{value}")
            else:
                value = block.get("text")
                if isinstance(value, str):
                    text_parts.append(value)
            continue
        if isinstance(block_type, str) and block_type in _RICH_BLOCK_TYPES:
            rich_blocks.append(copy.deepcopy(block))
            continue
        if isinstance(block_type, str) and block_type not in _NON_DISPLAY_BLOCK_TYPES:
            unknown_types.append(block_type or "<empty>")

    metadata_attachments: list[dict[str, Any]] = []
    if response_metadata is not None:
        raw_attachments = response_metadata.get("attachments")
        if isinstance(raw_attachments, list):
            for item in raw_attachments:
                if isinstance(item, Mapping):
                    file_id = item.get("file_id")
                    if isinstance(file_id, str) and file_id:
                        metadata_attachments.append(
                            {
                                str(key): copy.deepcopy(value)
                                for key, value in item.items()
                                if str(key) != "data_url"
                            }
                        )

    attachments = metadata_attachments or block_attachments
    return UserContentProjection(
        blocks=tuple(copy.deepcopy(block) for block in blocks),
        visible_text="".join(text_parts),
        attachments=tuple(attachments),
        rich_blocks=tuple(rich_blocks),
        unknown_block_types=tuple(unknown_types),
    )
