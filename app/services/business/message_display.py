from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.prompting.registry import (
    InternalDisplayPolicy,
    structured_prompt_registry,
)
from app.prompting.validation import internal_prompt_metadata, validate_internal_message

DISPLAY_CONTENT_METADATA_KEY = "display_content"
INTERNAL_DISPLAY_KIND_METADATA_KEY = "internal_display_kind"


@dataclass(frozen=True, slots=True)
class MessageDisplayProjection:
    visible: bool
    content: str
    metadata: dict[str, object]


def project_message_for_display(
    content: str,
    metadata: Mapping[str, object],
) -> MessageDisplayProjection:
    """按后端注册策略生成可发送给展示层的数据。"""
    structured_metadata = internal_prompt_metadata(metadata)
    if structured_metadata is None:
        display_content = metadata.get(DISPLAY_CONTENT_METADATA_KEY)
        if display_content is None:
            display_content = content
        if not isinstance(display_content, str):
            raise TypeError("message metadata.display_content 必须是字符串")
        return MessageDisplayProjection(
            visible=True,
            content=display_content,
            metadata=dict(metadata),
        )

    validate_internal_message(content, metadata)
    kind = structured_metadata["structured_prompt_kind"]
    if not isinstance(kind, str):
        raise TypeError("内部结构消息 structured_prompt_kind 必须是字符串")
    kind_spec = structured_prompt_registry.internal_message_kind(kind)
    public_metadata = {
        key: structured_metadata[key]
        for key in (
            "internal",
            "structured_prompt_kind",
            "structured_prompt_schema_version",
            "internal_display_kind",
        )
        if key in structured_metadata
    }
    if kind_spec.display_policy == InternalDisplayPolicy.hidden:
        return MessageDisplayProjection(
            visible=False,
            content="",
            metadata=public_metadata,
        )

    display_content = structured_metadata.get(DISPLAY_CONTENT_METADATA_KEY)
    if not isinstance(display_content, str) or not display_content.strip():
        raise ValueError(f"显式展示内部消息 kind={kind} 缺少 display_content")
    return MessageDisplayProjection(
        visible=True,
        content=display_content,
        metadata=public_metadata,
    )


def resolve_message_display_content(
    content: str,
    metadata: Mapping[str, object],
) -> str:
    return project_message_for_display(content, metadata).content


__all__ = [
    "DISPLAY_CONTENT_METADATA_KEY",
    "INTERNAL_DISPLAY_KIND_METADATA_KEY",
    "MessageDisplayProjection",
    "project_message_for_display",
    "resolve_message_display_content",
]
