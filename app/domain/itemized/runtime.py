"""v2 Turn、execution、model-call、provenance 与 content-part 领域定义。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.domain.itemized.enums import ControlOutcome, TurnStatus
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import _ensure_json_value, sha256_jcs
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.schema import (
    ITEM_STATUSES as _ITEM_STATUSES,
)
from app.domain.itemized.schema import (
    SEMANTIC_KINDS as _SEMANTIC_KINDS,
)


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ItemSchemaError(f"{field_name} 必须是非空字符串")
    return value

@dataclass(frozen=True, slots=True)
class TurnRecord:
    turn_id: str
    turn_ordinal: int
    source_branch_id: str
    root_input_item_id: str
    accepted_ingress_id: str
    acceptance_idempotency_key: str
    initial_execution_id: str
    status: str = TurnStatus.OPEN
    final_item_id: str | None = None
    replay_of_turn_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "turn_id",
            "source_branch_id",
            "root_input_item_id",
            "accepted_ingress_id",
            "acceptance_idempotency_key",
            "initial_execution_id",
        ):
            _non_empty_string(getattr(self, name), name)
        if not isinstance(self.turn_ordinal, int) or isinstance(self.turn_ordinal, bool) or self.turn_ordinal <= 0:
            raise ItemSchemaError("Turn.turn_ordinal 必须是正整数")
        if self.status not in {value.value for value in TurnStatus}:
            raise ItemSchemaError(f"未知 Turn.status: {self.status}")
        if self.final_item_id is not None:
            _non_empty_string(self.final_item_id, "final_item_id")
        if self.status == TurnStatus.COMPLETED and not self.final_item_id:
            raise ItemSchemaError("completed Turn 必须有 final_item_id")
        if self.status != TurnStatus.COMPLETED and self.final_item_id is not None:
            raise ItemSchemaError(
                "只有 completed Turn 可以保存 final_item_id"
            )


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    execution_id: str
    turn_id: str
    attempt: int
    outcome: str = ControlOutcome.UNKNOWN
    resumed_from_execution_id: str | None = None

    def __post_init__(self) -> None:
        _non_empty_string(self.execution_id, "execution_id")
        _non_empty_string(self.turn_id, "turn_id")
        if (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or self.attempt <= 0
        ):
            raise ItemSchemaError("execution attempt 必须是正整数")
        if self.outcome not in {value.value for value in ControlOutcome}:
            raise ItemSchemaError(f"未知 execution outcome: {self.outcome}")


@dataclass(frozen=True, slots=True)
class ModelCallRecord:
    model_call_id: str
    execution_id: str
    attempt: int
    provider: str
    outcome: str = ControlOutcome.UNKNOWN
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("model_call_id", "execution_id", "provider"):
            _non_empty_string(getattr(self, name), name)
        if (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or self.attempt <= 0
        ):
            raise ItemSchemaError("model call attempt 必须是正整数")
        if self.outcome not in {value.value for value in ControlOutcome}:
            raise ItemSchemaError(f"未知 model_call outcome: {self.outcome}")


@dataclass(frozen=True, slots=True)
class ProvenanceEdge:
    edge_id: str
    relation: str
    source_ref: str
    target_ref: str
    attempt: int | None = None
    supersedes_edge_id: str | None = None
    replay_input: bool = False

    def __post_init__(self) -> None:
        for name in ("edge_id", "relation", "source_ref", "target_ref"):
            _non_empty_string(getattr(self, name), name)
        if self.attempt is not None and (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or self.attempt <= 0
        ):
            raise ItemSchemaError("provenance attempt 必须是正整数")
        if self.supersedes_edge_id is not None:
            _non_empty_string(self.supersedes_edge_id, "supersedes_edge_id")


@dataclass(frozen=True, slots=True)
class ContentPart:
    """父 item payload 内的稳定 part；正文不在 SQLite 复制。"""

    part_id: str
    part_ordinal: int
    part_semantic_kind: str
    content: object
    content_hash: str
    prefix_hash: str | None = None

    def __post_init__(self) -> None:
        _non_empty_string(self.part_id, "ContentPart.part_id")
        if not isinstance(self.part_ordinal, int) or isinstance(self.part_ordinal, bool) or self.part_ordinal < 0:
            raise ItemSchemaError("content part ordinal 不能为负数")
        _non_empty_string(self.part_semantic_kind, "ContentPart.part_semantic_kind")
        if self.part_semantic_kind not in _SEMANTIC_KINDS:
            raise ItemSchemaError(f"未知 content part semantic kind: {self.part_semantic_kind}")
        if self.content_hash != sha256_jcs(self.content):
            raise ItemSchemaError("content part hash 与正文不一致")
        if self.prefix_hash is not None:
            _non_empty_string(self.prefix_hash, "ContentPart.prefix_hash")

    @classmethod
    def create(
        cls,
        *,
        part_id: str,
        part_ordinal: int,
        part_semantic_kind: str,
        content: object,
        prefix: object | None = None,
    ) -> ContentPart:
        if part_ordinal < 0:
            raise ItemSchemaError("content part ordinal 不能为负数")
        _non_empty_string(part_id, "part_id")
        _non_empty_string(part_semantic_kind, "part_semantic_kind")
        return cls(
            part_id=part_id,
            part_ordinal=part_ordinal,
            part_semantic_kind=part_semantic_kind,
            content=content,
            content_hash=sha256_jcs(content),
            prefix_hash=sha256_jcs(prefix) if prefix is not None else None,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "part_id": self.part_id,
            "part_ordinal": self.part_ordinal,
            "part_semantic_kind": self.part_semantic_kind,
            "content": self.content,
            "content_hash": self.content_hash,
        }
        if self.prefix_hash is not None:
            result["prefix_hash"] = self.prefix_hash
        return result


@dataclass(frozen=True, slots=True)
class ContentPartAnchor:
    """durable part anchor；未声明 recovery capability 的 fragment 不可操作。"""

    anchor_id: str
    item_id: str
    part_id: str
    mode: str
    view_id: str
    branch_id: str
    capability: str
    content_hash: str
    prefix_hash: str | None = None
    fragment_identity: str | None = None
    fragment_length: int | None = None
    fragment_hash: str | None = None
    fragment_layout: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        for name in (
            "anchor_id",
            "item_id",
            "part_id",
            "view_id",
            "branch_id",
            "capability",
            "content_hash",
        ):
            _non_empty_string(getattr(self, name), name)
        if self.mode not in {"before", "inclusive"}:
            raise ItemSchemaError("content part anchor mode 必须是 before/inclusive")
        if self.capability not in {"content_part", "fragment"}:
            raise ItemSchemaError("未知 content part anchor capability")
        if self.prefix_hash is not None:
            _non_empty_string(self.prefix_hash, "prefix_hash")
        if self.capability == "fragment":
            _non_empty_string(self.fragment_identity, "fragment_identity")
            if (
                not isinstance(self.fragment_length, int)
                or isinstance(self.fragment_length, bool)
                or self.fragment_length < 0
            ):
                raise ItemSchemaError(
                    "fragment anchor 必须包含非负 fragment_length"
                )
            _non_empty_string(self.fragment_hash, "fragment_hash")
            if not isinstance(self.fragment_layout, Mapping) or not self.fragment_layout:
                raise ItemSchemaError(
                    "fragment anchor 必须包含可恢复的 fragment_layout"
                )
            _ensure_json_value(self.fragment_layout, "fragment_layout")
        elif any(
            value is not None
            for value in (
                self.fragment_identity,
                self.fragment_length,
                self.fragment_hash,
                self.fragment_layout,
            )
        ):
            raise ItemSchemaError(
                "content_part anchor 不得携带 fragment 专用字段"
            )


@dataclass(slots=True)
class ItemDraft:
    item_id: str
    semantic_kind: str
    payload_kind: str
    producer_ref: Mapping[str, object]
    payload: object
    metadata: dict[str, object] = field(default_factory=dict)
    status: str | None = None
    turn_id: str | None = None
    turn_scope: str | None = None
    message_group_id: str | None = None
    wire_role: str | None = None

    def finalize(self, status: str, *, item_sequence: int = 1) -> CanonicalItemRecord:
        if self.status is not None:
            raise ItemSchemaError("ItemDraft 不能二次终态化")
        if status not in _ITEM_STATUSES:
            raise ItemSchemaError(f"未知 draft status: {status}")
        self.status = status
        return CanonicalItemRecord.create(
            item_sequence=item_sequence,
            item_id=self.item_id,
            semantic_kind=self.semantic_kind,
            payload_kind=self.payload_kind,
            status=status,
            producer_ref=self.producer_ref,
            payload=self.payload,
            metadata=self.metadata,
            turn_id=self.turn_id,
            turn_scope=self.turn_scope,
            message_group_id=self.message_group_id,
            wire_role=self.wire_role,
        )

__all__ = ["ContentPart", "ContentPartAnchor", "ExecutionRecord", "ItemDraft", "ModelCallRecord", "ProvenanceEdge", "TurnRecord"]
