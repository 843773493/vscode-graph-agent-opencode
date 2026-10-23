from __future__ import annotations

import json
from collections.abc import Mapping
from xml.etree import ElementTree

from app.prompting.factory import (
    INTERNAL_PROMPT_KIND_METADATA_KEY,
    INTERNAL_PROMPT_SCHEMA_VERSION,
    INTERNAL_PROMPT_VERSION_METADATA_KEY,
)
from app.prompting.registry import (
    InternalDisplayPolicy,
    PromptContentCodec,
    PromptPlacement,
    StructuredPromptRegistry,
    structured_prompt_registry,
)

_STRUCTURAL_PROMPT_ATTRIBUTES = frozenset({"encoding", "trust"})


def internal_prompt_metadata(
    metadata: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    if not metadata:
        return None
    if INTERNAL_PROMPT_VERSION_METADATA_KEY in metadata:
        return metadata
    nested = metadata.get("message_metadata")
    if isinstance(nested, Mapping) and INTERNAL_PROMPT_VERSION_METADATA_KEY in nested:
        merged = dict(nested)
        for key in ("display_content", "internal_display_kind"):
            if key in metadata:
                merged[key] = metadata[key]
        return merged
    return None


def validate_internal_message(
    content: object,
    metadata: Mapping[str, object] | None,
    *,
    registry: StructuredPromptRegistry = structured_prompt_registry,
) -> None:
    structured_metadata = internal_prompt_metadata(metadata)
    if structured_metadata is None:
        return
    if not isinstance(content, str):
        raise TypeError("内部结构消息内容必须是字符串")
    version = structured_metadata.get(INTERNAL_PROMPT_VERSION_METADATA_KEY)
    if version != INTERNAL_PROMPT_SCHEMA_VERSION:
        raise ValueError(f"不支持的内部结构消息 schema version: {version}")
    kind = structured_metadata.get(INTERNAL_PROMPT_KIND_METADATA_KEY)
    if not isinstance(kind, str) or not kind:
        raise ValueError("内部结构消息缺少 structured_prompt_kind")
    if structured_metadata.get("internal") is not True:
        raise ValueError("内部结构消息 metadata.internal 必须为 true")
    kind_spec = registry.internal_message_kind(kind)
    display_content = structured_metadata.get("display_content")
    display_kind = structured_metadata.get("internal_display_kind")
    if kind_spec.display_policy == InternalDisplayPolicy.explicit:
        if not isinstance(display_content, str) or not display_content.strip():
            raise ValueError(f"内部结构消息 kind={kind} 缺少非空 display_content")
        if display_kind != kind_spec.display_kind:
            raise ValueError(
                f"内部结构消息 kind={kind} 的 internal_display_kind 与注册值不一致"
            )
    elif display_content is not None or display_kind is not None:
        raise ValueError(f"内部结构消息 kind={kind} 的隐藏展示策略包含展示 metadata")

    try:
        system_reminder_element = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"内部结构消息不是合法标记结构: {exc}") from exc
    if system_reminder_element.tag != "system_reminder":
        raise ValueError(
            "内部结构消息根标签必须是 system_reminder: "
            f"{system_reminder_element.tag}"
        )
    if system_reminder_element.attrib:
        raise ValueError("system_reminder 根标签不允许属性")
    system_reminder_spec = registry.tag(system_reminder_element.tag)
    if system_reminder_spec.placement != PromptPlacement.internal_human:
        raise ValueError("system_reminder 注册 placement 错误")

    section_names: set[str] = set()
    duplicate_section_names: set[str] = set()
    for section_element in system_reminder_element:
        section_spec = registry.tag(section_element.tag)
        if section_spec.allowed_parent != system_reminder_element.tag:
            raise ValueError(
                f"标签 {section_element.tag} 不允许位于 "
                f"{system_reminder_element.tag}"
            )
        unsupported_attributes = (
            set(section_element.attrib)
            - _STRUCTURAL_PROMPT_ATTRIBUTES
            - section_spec.allowed_attributes
        )
        if unsupported_attributes:
            raise ValueError(
                f"标签 {section_element.tag} 包含未注册属性: "
                f"{sorted(unsupported_attributes)}"
            )
        if section_element.attrib.get("encoding") != section_spec.codec.value:
            raise ValueError(
                f"标签 {section_element.tag} 的 encoding 与注册 codec 不一致"
            )
        if section_element.attrib.get("trust") != section_spec.trust_level.value:
            raise ValueError(
                f"标签 {section_element.tag} 的 trust 与注册级别不一致"
            )
        if len(section_element):
            raise ValueError(f"数据标签 {section_element.tag} 不允许嵌套子标签")
        if section_spec.codec == PromptContentCodec.json:
            try:
                json.loads(section_element.text or "")
            except json.JSONDecodeError as exc:
                raise ValueError(f"标签 {section_element.tag} 包含无效 JSON") from exc
        if section_element.tag in section_names:
            duplicate_section_names.add(section_element.tag)
        else:
            section_names.add(section_element.tag)

    duplicates = sorted(duplicate_section_names)
    if duplicates:
        raise ValueError(f"内部结构消息包含重复 section: {duplicates}")
    unknown_sections = section_names - kind_spec.allowed_sections
    if unknown_sections:
        raise ValueError(
            f"内部结构消息 kind={kind} 包含非法 section: {sorted(unknown_sections)}"
        )
    missing_sections = kind_spec.required_sections - section_names
    if missing_sections:
        raise ValueError(
            f"内部结构消息 kind={kind} 缺少 section: {sorted(missing_sections)}"
        )


__all__ = ["internal_prompt_metadata", "validate_internal_message"]
