from __future__ import annotations

import json
from collections import Counter
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
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"内部结构消息不是合法标记结构: {exc}") from exc
    if root.tag != "system_reminder":
        raise ValueError(f"内部结构消息根标签必须是 system_reminder: {root.tag}")
    if root.attrib:
        raise ValueError("system_reminder 根标签不允许属性")
    root_spec = registry.tag(root.tag)
    if root_spec.placement != PromptPlacement.internal_human:
        raise ValueError("system_reminder 注册 placement 错误")

    section_names: list[str] = []
    for child in root:
        spec = registry.tag(child.tag)
        if spec.allowed_parent != root.tag:
            raise ValueError(f"标签 {child.tag} 不允许位于 {root.tag}")
        structural_attributes = {"encoding", "trust"}
        unsupported_attributes = (
            set(child.attrib) - structural_attributes - spec.allowed_attributes
        )
        if unsupported_attributes:
            raise ValueError(
                f"标签 {child.tag} 包含未注册属性: {sorted(unsupported_attributes)}"
            )
        if child.attrib.get("encoding") != spec.codec.value:
            raise ValueError(f"标签 {child.tag} 的 encoding 与注册 codec 不一致")
        if child.attrib.get("trust") != spec.trust_level.value:
            raise ValueError(f"标签 {child.tag} 的 trust 与注册级别不一致")
        if list(child):
            raise ValueError(f"数据标签 {child.tag} 不允许嵌套子标签")
        if spec.codec == PromptContentCodec.json:
            try:
                json.loads(child.text or "")
            except json.JSONDecodeError as exc:
                raise ValueError(f"标签 {child.tag} 包含无效 JSON") from exc
        section_names.append(child.tag)

    duplicates = sorted(
        name for name, count in Counter(section_names).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"内部结构消息包含重复 section: {duplicates}")
    unknown_sections = set(section_names) - kind_spec.allowed_sections
    if unknown_sections:
        raise ValueError(
            f"内部结构消息 kind={kind} 包含非法 section: {sorted(unknown_sections)}"
        )
    missing_sections = kind_spec.required_sections - set(section_names)
    if missing_sections:
        raise ValueError(
            f"内部结构消息 kind={kind} 缺少 section: {sorted(missing_sections)}"
        )


__all__ = ["internal_prompt_metadata", "validate_internal_message"]
