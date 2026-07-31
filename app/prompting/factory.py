from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape

from app.abstractions.internal_message import PreparedInternalMessage
from app.prompting.registry import (
    InternalDisplayPolicy,
    PromptContentCodec,
    PromptPlacement,
    StructuredPromptRegistry,
    structured_prompt_registry,
)
from app.prompting.serialization import serialize_prompt_json

INTERNAL_PROMPT_SCHEMA_VERSION = 2
INTERNAL_PROMPT_KIND_METADATA_KEY = "structured_prompt_kind"
INTERNAL_PROMPT_VERSION_METADATA_KEY = "structured_prompt_schema_version"


@dataclass(frozen=True, slots=True)
class PromptSection:
    tag: str
    value: object
    attributes: Mapping[str, str] | None = None


class InternalMessageFactory:
    def __init__(self, registry: StructuredPromptRegistry) -> None:
        self._registry = registry

    def build(
        self,
        *,
        kind: str,
        control: str,
        sections: Sequence[PromptSection] = (),
        metadata: Mapping[str, object] | None = None,
        display_content: str | None = None,
    ) -> PreparedInternalMessage:
        kind_spec = self._registry.internal_message_kind(kind)
        section_names = [section.tag for section in sections]
        duplicate_names = {
            name for name in section_names if section_names.count(name) > 1
        }
        if duplicate_names:
            raise ValueError(
                f"内部结构消息不允许重复 section: {sorted(duplicate_names)}"
            )
        unknown_sections = set(section_names) - kind_spec.allowed_sections
        if unknown_sections:
            raise ValueError(
                f"内部结构消息 kind={kind} 不允许 section: {sorted(unknown_sections)}"
            )
        missing_sections = kind_spec.required_sections - set(section_names)
        if missing_sections:
            raise ValueError(
                f"内部结构消息 kind={kind} 缺少 section: {sorted(missing_sections)}"
            )

        rendered_sections = [self._render_section(section) for section in sections]
        body_parts = [escape(control, quote=False).strip(), *rendered_sections]
        body = "\n".join(part for part in body_parts if part)
        content = f"<system_reminder>\n{body}\n</system_reminder>"

        resolved_metadata = dict(metadata or {})
        reserved_keys = {
            "internal",
            INTERNAL_PROMPT_KIND_METADATA_KEY,
            INTERNAL_PROMPT_VERSION_METADATA_KEY,
            "display_content",
            "internal_display_kind",
        }
        conflicting_keys = reserved_keys & resolved_metadata.keys()
        if conflicting_keys:
            raise ValueError(
                "内部结构消息 metadata 不得覆盖保留字段: "
                f"{sorted(conflicting_keys)}"
            )
        resolved_metadata.update(
            {
                "internal": True,
                INTERNAL_PROMPT_KIND_METADATA_KEY: kind,
                INTERNAL_PROMPT_VERSION_METADATA_KEY: (
                    INTERNAL_PROMPT_SCHEMA_VERSION
                ),
            }
        )
        if display_content is not None:
            resolved_metadata["display_content"] = display_content
        if kind_spec.display_policy == InternalDisplayPolicy.explicit:
            if not isinstance(display_content, str) or not display_content.strip():
                raise ValueError(
                    f"内部结构消息 kind={kind} 必须提供非空 display_content"
                )
            resolved_metadata["internal_display_kind"] = kind_spec.display_kind
        elif display_content is not None:
            raise ValueError(
                f"内部结构消息 kind={kind} 使用隐藏展示策略，不能提供 display_content"
            )
        return PreparedInternalMessage(
            content=content,
            metadata=resolved_metadata,
        )

    def render_system_prompt_section(self, section: PromptSection) -> str:
        spec = self._registry.tag(section.tag)
        if spec.placement != PromptPlacement.system_prompt:
            raise ValueError(
                f"标签 {section.tag} 不能作为独立 system prompt section"
            )
        return self._render_section(section, expected_parent=None)

    def _render_section(
        self,
        section: PromptSection,
        *,
        expected_parent: str | None = "system_reminder",
    ) -> str:
        spec = self._registry.tag(section.tag)
        if spec.allowed_parent != expected_parent:
            raise ValueError(
                f"标签 {section.tag} 不允许位于 {expected_parent or '根级'}"
            )
        custom_attributes = dict(section.attributes or {})
        reserved_attributes = {"encoding", "trust"} & custom_attributes.keys()
        if reserved_attributes:
            raise ValueError(
                f"标签 {section.tag} 不允许覆盖结构属性: "
                f"{sorted(reserved_attributes)}"
            )
        unsupported_attributes = set(custom_attributes) - spec.allowed_attributes
        if unsupported_attributes:
            raise ValueError(
                f"标签 {section.tag} 不支持属性: {sorted(unsupported_attributes)}"
            )
        attributes = {
            "encoding": spec.codec.value,
            "trust": spec.trust_level.value,
            **custom_attributes,
        }
        rendered_attributes = "".join(
            f' {name}="{escape(value, quote=True)}"'
            for name, value in sorted(attributes.items())
        )
        if spec.codec == PromptContentCodec.text:
            if not isinstance(section.value, str):
                raise TypeError(f"标签 {section.tag} 的内容必须是字符串")
            rendered_value = escape(section.value, quote=False)
        elif spec.codec == PromptContentCodec.json:
            rendered_value = serialize_prompt_json(section.value)
        else:
            raise ValueError(f"标签 {section.tag} 不能作为数据 section 渲染")
        return (
            f"<{section.tag}{rendered_attributes}>\n"
            f"{rendered_value}\n"
            f"</{section.tag}>"
        )


internal_message_factory = InternalMessageFactory(structured_prompt_registry)


__all__ = [
    "INTERNAL_PROMPT_KIND_METADATA_KEY",
    "INTERNAL_PROMPT_SCHEMA_VERSION",
    "INTERNAL_PROMPT_VERSION_METADATA_KEY",
    "InternalMessageFactory",
    "PromptSection",
    "internal_message_factory",
]
