from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

_T = TypeVar("_T")
_TAG_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class PromptTrustLevel(StrEnum):
    control = "control"
    workspace_instruction = "workspace_instruction"
    untrusted_data = "untrusted_data"
    untrusted_reference = "untrusted_reference"


class PromptContentCodec(StrEnum):
    mixed = "mixed"
    text = "text"
    json = "json"


class PromptPlacement(StrEnum):
    internal_human = "internal_human"
    system_prompt = "system_prompt"


class InternalDisplayPolicy(StrEnum):
    hidden = "hidden"
    explicit = "explicit"


@dataclass(frozen=True, slots=True)
class PromptTagSpec:
    name: str
    trust_level: PromptTrustLevel
    codec: PromptContentCodec
    placement: PromptPlacement
    allowed_parent: str | None
    allowed_attributes: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class InternalMessageKindSpec:
    kind: str
    allowed_sections: frozenset[str]
    required_sections: frozenset[str] = frozenset()
    display_policy: InternalDisplayPolicy = InternalDisplayPolicy.hidden
    display_kind: str | None = None


class StructuredPromptRegistry:
    def __init__(
        self,
        *,
        tags: tuple[PromptTagSpec, ...],
        internal_message_kinds: tuple[InternalMessageKindSpec, ...],
    ) -> None:
        self._tags = self._index_unique(
            tags, key_name="标签", key=lambda item: item.name
        )
        self._internal_message_kinds = self._index_unique(
            internal_message_kinds,
            key_name="内部消息 kind",
            key=lambda item: item.kind,
        )
        self._validate_registration()

    @staticmethod
    def _index_unique(
        items: tuple[_T, ...],
        *,
        key_name: str,
        key: Callable[[_T], str],
    ) -> dict[str, _T]:
        indexed: dict[str, _T] = {}
        for item in items:
            item_key = key(item)
            if item_key in indexed:
                raise ValueError(f"结构化提示{key_name}重复注册: {item_key}")
            indexed[item_key] = item
        return indexed

    def _validate_registration(self) -> None:
        reminder = self._tags.get("system_reminder")
        if reminder is None:
            raise ValueError("结构化提示注册表缺少 system_reminder 根标签")
        if (
            reminder.allowed_parent is not None
            or reminder.placement != PromptPlacement.internal_human
            or reminder.codec != PromptContentCodec.mixed
        ):
            raise ValueError("system_reminder 必须注册为 internal_human/mixed 根标签")
        for tag in self._tags.values():
            if _TAG_NAME_PATTERN.fullmatch(tag.name) is None:
                raise ValueError(f"结构化提示标签名无效: {tag.name}")
            if tag.allowed_parent is not None and tag.allowed_parent not in self._tags:
                raise ValueError(
                    f"标签 {tag.name} 引用了未注册父标签: {tag.allowed_parent}"
                )
            if tag.name != "system_reminder":
                if tag.codec == PromptContentCodec.mixed:
                    raise ValueError(f"数据标签 {tag.name} 不得使用 mixed codec")
                if tag.placement == PromptPlacement.internal_human:
                    if tag.allowed_parent != "system_reminder":
                        raise ValueError(
                            f"内部消息标签 {tag.name} 必须直属 system_reminder"
                        )
                elif tag.allowed_parent is not None:
                    raise ValueError(f"system prompt 标签 {tag.name} 必须注册为根标签")
            reserved_attributes = {"encoding", "trust"} & tag.allowed_attributes
            if reserved_attributes:
                raise ValueError(
                    f"标签 {tag.name} 不得注册结构保留属性: "
                    f"{sorted(reserved_attributes)}"
                )
        for kind in self._internal_message_kinds.values():
            if _TAG_NAME_PATTERN.fullmatch(kind.kind) is None:
                raise ValueError(f"内部消息 kind 名无效: {kind.kind}")
            if not kind.required_sections <= kind.allowed_sections:
                raise ValueError(
                    f"内部消息 kind={kind.kind} 的 required_sections "
                    "必须属于 allowed_sections"
                )
            for section_name in kind.allowed_sections:
                section = self.tag(section_name)
                if section.allowed_parent != "system_reminder":
                    raise ValueError(
                        f"内部消息 kind={kind.kind} 引用了非 reminder section: "
                        f"{section_name}"
                    )
            if kind.display_policy == InternalDisplayPolicy.explicit:
                if not kind.display_kind:
                    raise ValueError(
                        f"内部消息 kind={kind.kind} 的显式展示策略缺少 display_kind"
                    )
                if _TAG_NAME_PATTERN.fullmatch(kind.display_kind) is None:
                    raise ValueError(
                        f"内部消息 kind={kind.kind} 的 display_kind 名无效: "
                        f"{kind.display_kind}"
                    )
            elif kind.display_kind is not None:
                raise ValueError(
                    f"内部消息 kind={kind.kind} 的隐藏展示策略不得声明 display_kind"
                )

    def tag(self, name: str) -> PromptTagSpec:
        try:
            return self._tags[name]
        except KeyError as exc:
            raise ValueError(f"未注册的结构化提示标签: {name}") from exc

    def internal_message_kind(self, kind: str) -> InternalMessageKindSpec:
        try:
            return self._internal_message_kinds[kind]
        except KeyError as exc:
            raise ValueError(f"未注册的内部结构消息 kind: {kind}") from exc


TAG_SPECS = (
    PromptTagSpec(
        "system_reminder",
        PromptTrustLevel.control,
        PromptContentCodec.mixed,
        PromptPlacement.internal_human,
        None,
    ),
    PromptTagSpec(
        "control_context",
        PromptTrustLevel.control,
        PromptContentCodec.json,
        PromptPlacement.internal_human,
        "system_reminder",
    ),
    PromptTagSpec(
        "untrusted_objective",
        PromptTrustLevel.untrusted_data,
        PromptContentCodec.text,
        PromptPlacement.internal_human,
        "system_reminder",
    ),
    PromptTagSpec(
        "delegated_task",
        PromptTrustLevel.untrusted_data,
        PromptContentCodec.text,
        PromptPlacement.internal_human,
        "system_reminder",
    ),
    PromptTagSpec(
        "generated_session_result",
        PromptTrustLevel.untrusted_data,
        PromptContentCodec.text,
        PromptPlacement.internal_human,
        "system_reminder",
    ),
    PromptTagSpec(
        "session_message",
        PromptTrustLevel.untrusted_data,
        PromptContentCodec.text,
        PromptPlacement.internal_human,
        "system_reminder",
    ),
    PromptTagSpec(
        "untrusted_instructions",
        PromptTrustLevel.untrusted_data,
        PromptContentCodec.text,
        PromptPlacement.internal_human,
        "system_reminder",
    ),
    PromptTagSpec(
        "team_task",
        PromptTrustLevel.untrusted_data,
        PromptContentCodec.json,
        PromptPlacement.internal_human,
        "system_reminder",
    ),
    PromptTagSpec(
        "team_task_update",
        PromptTrustLevel.untrusted_data,
        PromptContentCodec.json,
        PromptPlacement.internal_human,
        "system_reminder",
    ),
    PromptTagSpec(
        "workspace_agents_md_change",
        PromptTrustLevel.workspace_instruction,
        PromptContentCodec.text,
        PromptPlacement.internal_human,
        "system_reminder",
        frozenset({"path"}),
    ),
    PromptTagSpec(
        "workspace_agents_md",
        PromptTrustLevel.workspace_instruction,
        PromptContentCodec.text,
        PromptPlacement.system_prompt,
        None,
        frozenset({"path"}),
    ),
    PromptTagSpec(
        "agent_memory",
        PromptTrustLevel.untrusted_reference,
        PromptContentCodec.text,
        PromptPlacement.system_prompt,
        None,
    ),
)


INTERNAL_MESSAGE_KIND_SPECS = (
    InternalMessageKindSpec(
        "goal_continuation",
        frozenset({"untrusted_objective"}),
        frozenset({"untrusted_objective"}),
    ),
    InternalMessageKindSpec(
        "goal_objective_updated",
        frozenset({"untrusted_objective"}),
        frozenset({"untrusted_objective"}),
    ),
    InternalMessageKindSpec(
        "goal_budget_limited",
        frozenset({"untrusted_objective"}),
        frozenset({"untrusted_objective"}),
    ),
    InternalMessageKindSpec(
        "delegated_task",
        frozenset({"control_context", "delegated_task", "untrusted_instructions"}),
        frozenset({"control_context", "delegated_task"}),
        InternalDisplayPolicy.explicit,
        "delegated_task",
    ),
    InternalMessageKindSpec(
        "generated_session_result",
        frozenset({"control_context", "generated_session_result"}),
        frozenset({"control_context", "generated_session_result"}),
        InternalDisplayPolicy.explicit,
        "generated_session_result",
    ),
    InternalMessageKindSpec(
        "session_message",
        frozenset({"control_context", "session_message"}),
        frozenset({"control_context", "session_message"}),
    ),
    InternalMessageKindSpec(
        "team_membership",
        frozenset({"control_context", "untrusted_instructions"}),
        frozenset({"control_context"}),
    ),
    InternalMessageKindSpec(
        "team_task_assignment",
        frozenset({"control_context", "team_task"}),
        frozenset({"control_context", "team_task"}),
    ),
    InternalMessageKindSpec(
        "team_task_update",
        frozenset({"control_context", "team_task_update"}),
        frozenset({"control_context", "team_task_update"}),
    ),
    InternalMessageKindSpec(
        "workspace_agents_change",
        frozenset({"workspace_agents_md_change"}),
        frozenset({"workspace_agents_md_change"}),
    ),
    InternalMessageKindSpec("checkpoint_reminder", frozenset()),
    InternalMessageKindSpec(
        "missing_custom_tool_retry", frozenset({"control_context"})
    ),
    InternalMessageKindSpec("delegated_report_retry", frozenset({"control_context"})),
    InternalMessageKindSpec(
        "session_question_reply_retry", frozenset({"control_context"})
    ),
    InternalMessageKindSpec("empty_response_retry", frozenset({"control_context"})),
    InternalMessageKindSpec("tool_test_retry", frozenset()),
    InternalMessageKindSpec("compaction_summary_instruction", frozenset()),
    InternalMessageKindSpec("compaction_retry_marker", frozenset()),
)


structured_prompt_registry = StructuredPromptRegistry(
    tags=TAG_SPECS,
    internal_message_kinds=INTERNAL_MESSAGE_KIND_SPECS,
)


__all__ = [
    "InternalDisplayPolicy",
    "InternalMessageKindSpec",
    "PromptContentCodec",
    "PromptPlacement",
    "PromptTagSpec",
    "PromptTrustLevel",
    "StructuredPromptRegistry",
    "structured_prompt_registry",
]
