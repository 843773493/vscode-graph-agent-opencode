from __future__ import annotations

import json
from collections.abc import Mapping
from xml.etree import ElementTree

from langchain_core.messages import BaseMessage

from app.prompting.factory import (
    INTERNAL_PROMPT_KIND_METADATA_KEY,
    INTERNAL_PROMPT_VERSION_METADATA_KEY,
    PromptSection,
    internal_message_factory,
)
from app.prompting.registry import (
    InternalDisplayPolicy,
    PromptContentCodec,
    structured_prompt_registry,
)


def migrate_internal_message_v1(message: BaseMessage) -> bool:
    """把 checkpoint 中已落盘的 v1 内部消息原位升级为当前结构。"""
    response_metadata = dict(message.response_metadata or {})
    nested = response_metadata.get("message_metadata")
    nested_metadata = dict(nested) if isinstance(nested, Mapping) else None
    source_metadata = nested_metadata or response_metadata
    if source_metadata.get(INTERNAL_PROMPT_VERSION_METADATA_KEY) != 1:
        return False

    content = message.content
    if not isinstance(content, str):
        raise TypeError("v1 内部结构消息内容必须是字符串")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"v1 内部结构消息不是合法标记结构: {exc}") from exc
    if root.tag != "system_reminder":
        raise ValueError(f"v1 内部结构消息根标签无效: {root.tag}")

    kind = source_metadata.get(INTERNAL_PROMPT_KIND_METADATA_KEY)
    if not isinstance(kind, str) or not kind:
        raise ValueError("v1 内部结构消息缺少 structured_prompt_kind")
    sections: list[PromptSection] = []
    for child in root:
        value: object = child.text or ""
        tag_spec = structured_prompt_registry.tag(child.tag)
        if tag_spec.codec == PromptContentCodec.json:
            try:
                value = json.loads(child.text or "")
            except json.JSONDecodeError as exc:
                raise ValueError(f"v1 标签 {child.tag} 包含无效 JSON") from exc
        sections.append(
            PromptSection(
                child.tag,
                value,
                attributes={
                    name: attribute_value
                    for name, attribute_value in child.attrib.items()
                    if name not in {"encoding", "trust"}
                },
            )
        )
    sections = _upgrade_v1_section_semantics(kind, sections)

    reserved = {
        "internal",
        INTERNAL_PROMPT_KIND_METADATA_KEY,
        INTERNAL_PROMPT_VERSION_METADATA_KEY,
        "display_content",
        "internal_display_kind",
    }
    prepared = internal_message_factory.build(
        kind=kind,
        control=(root.text or "").strip(),
        sections=tuple(sections),
        metadata=_upgrade_v1_metadata_semantics(
            kind,
            {
                key: value
                for key, value in source_metadata.items()
                if key not in reserved
            },
        ),
        display_content=_display_content(response_metadata, source_metadata),
    )
    message.content = prepared.content
    if nested_metadata is None:
        message.response_metadata = dict(prepared.metadata)
    else:
        migrated_nested = dict(prepared.metadata)
        display_content = migrated_nested.pop("display_content", None)
        message.response_metadata = {
            **response_metadata,
            "message_metadata": migrated_nested,
        }
        if display_content is not None:
            message.response_metadata["display_content"] = display_content
    return True


def _upgrade_v1_section_semantics(
    kind: str,
    sections: list[PromptSection],
) -> list[PromptSection]:
    upgraded: list[PromptSection] = []
    for section in sections:
        if section.tag != "control_context" or not isinstance(section.value, dict):
            upgraded.append(section)
            continue
        control = dict(section.value)
        data_sections: list[PromptSection] = []
        if kind == "delegated_task":
            trusted_context = control.get("trusted_context")
            if isinstance(trusted_context, dict):
                sanitized_context = dict(trusted_context)
                instructions = sanitized_context.pop("instructions", None)
                control["trusted_context"] = sanitized_context
                if isinstance(instructions, str) and instructions:
                    data_sections.append(
                        PromptSection("untrusted_instructions", instructions)
                    )
        elif kind == "team_membership":
            instructions = control.pop("instructions", None)
            if isinstance(instructions, str) and instructions:
                data_sections.append(
                    PromptSection("untrusted_instructions", instructions)
                )
        elif kind == "team_task_assignment":
            task = control.pop("task", None)
            if task is not None:
                data_sections.append(PromptSection("team_task", task))
        elif kind == "team_task_update":
            update = {
                key: control.pop(key)
                for key in ("status", "summary", "error")
                if key in control
            }
            if update:
                data_sections.append(PromptSection("team_task_update", update))
        upgraded.extend((PromptSection("control_context", control), *data_sections))
    return upgraded


def _upgrade_v1_metadata_semantics(
    kind: str,
    metadata: dict[str, object],
) -> dict[str, object]:
    if kind != "delegated_task":
        return metadata
    trusted_context = metadata.get("trusted_context")
    if not isinstance(trusted_context, dict) or "instructions" not in trusted_context:
        return metadata
    sanitized = dict(metadata)
    sanitized_context = dict(trusted_context)
    sanitized_context.pop("instructions")
    sanitized["trusted_context"] = sanitized_context
    return sanitized


def migrate_internal_messages_v1(value: object) -> int:
    if isinstance(value, BaseMessage):
        return int(_migrate_checkpoint_message(value))
    if not isinstance(value, list):
        return 0
    return sum(
        1
        for item in value
        if isinstance(item, BaseMessage) and _migrate_checkpoint_message(item)
    )


def _migrate_checkpoint_message(message: BaseMessage) -> bool:
    display_changed = _remove_hidden_display_metadata(message)
    schema_changed = migrate_internal_message_v1(message)
    return display_changed or schema_changed


def _remove_hidden_display_metadata(message: BaseMessage) -> bool:
    response_metadata = dict(message.response_metadata or {})
    nested = response_metadata.get("message_metadata")
    source = nested if isinstance(nested, Mapping) else response_metadata
    kind = source.get(INTERNAL_PROMPT_KIND_METADATA_KEY)
    if not isinstance(kind, str):
        return False
    kind_spec = structured_prompt_registry.internal_message_kind(kind)
    if kind_spec.display_policy != InternalDisplayPolicy.hidden:
        return False

    changed = False
    for key in ("display_content", "internal_display_kind"):
        if key in response_metadata:
            response_metadata.pop(key)
            changed = True
    if isinstance(nested, Mapping):
        migrated_nested = dict(nested)
        for key in ("display_content", "internal_display_kind"):
            if key in migrated_nested:
                migrated_nested.pop(key)
                changed = True
        response_metadata["message_metadata"] = migrated_nested
    if changed:
        message.response_metadata = response_metadata
    return changed


def migrate_prompt_checkpoint_channel_value(channel: str, value: object) -> None:
    if channel == "messages":
        migrate_internal_messages_v1(value)


def _display_content(
    response_metadata: Mapping[str, object],
    source_metadata: Mapping[str, object],
) -> str | None:
    value = response_metadata.get("display_content")
    if value is None:
        value = source_metadata.get("display_content")
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("v1 内部结构消息 display_content 必须是字符串")
    return value


__all__ = [
    "migrate_internal_message_v1",
    "migrate_internal_messages_v1",
    "migrate_prompt_checkpoint_channel_value",
]
