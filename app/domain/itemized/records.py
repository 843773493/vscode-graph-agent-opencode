"""v2 canonical item 与 producer record 的领域定义。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.errors import FormatDispatchError, ItemSchemaError
from app.domain.itemized.hashing import (
    _ensure_json_value,
    content_hash,
)
from app.domain.itemized.schema import (
    CORE_FIELDS as _CORE_FIELDS,
)
from app.domain.itemized.schema import (
    TOOL_OUTCOMES as _TOOL_OUTCOMES,
)
from app.domain.itemized.schema import (
    validate_extension_payload,
    validate_item_compatibility,
    validate_producer_ref,
)


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ItemSchemaError(f"{field_name} 必须是非空字符串")
    return value


def _validate_producer(value: object) -> dict[str, object]:
    return validate_producer_ref(value)


def _validate_payload(
    semantic_kind: str,
    payload_kind: str,
    status: str,
    payload: object,
    metadata: Mapping[str, object],
) -> None:
    _ensure_json_value(payload)
    if payload_kind == PayloadKind.TEXT and not isinstance(payload, str):
        raise ItemSchemaError("text payload 必须是字符串")
    if payload_kind == PayloadKind.STRUCTURED_CONTENT and not isinstance(
        payload, (Mapping, list, tuple)
    ):
        raise ItemSchemaError("structured_content payload 必须是 object 或 array")
    if payload_kind == PayloadKind.SUMMARY and not isinstance(payload, Mapping):
        raise ItemSchemaError("summary payload 必须是 object")
    if payload_kind in {PayloadKind.OPAQUE, PayloadKind.EXTENSION}:
        validate_extension_payload(payload, payload_kind=payload_kind)
        if semantic_kind in {SemanticKind.REASONING, SemanticKind.EXTENSION} and (
            not isinstance(payload.get("protection"), Mapping) or not payload["protection"]
        ):
            raise ItemSchemaError("item-schema-incompatible: payload 缺少 protection metadata")
        if semantic_kind == SemanticKind.EXTENSION:
            for name in ("extension_schema", "extension_version"):
                _non_empty_string(payload.get(name), f"item-schema-incompatible: payload.{name}")
        if payload.get("protection") is not None and not isinstance(
            payload["protection"], Mapping
        ):
            raise ItemSchemaError("payload.protection 必须是 object")
    if semantic_kind == SemanticKind.TOOL_CALL:
        if not isinstance(payload, Mapping):
            raise ItemSchemaError("tool_call payload 必须是 object")
        calls = payload.get("tool_calls")
        if isinstance(calls, list):
            if not calls:
                raise ItemSchemaError("tool_call payload 的 tool_calls 不能为空")
            for call in calls:
                if not isinstance(call, Mapping):
                    raise ItemSchemaError("tool_call payload 的 tool_calls 元素非法")
                _non_empty_string(call.get("id"), "payload.tool_calls[].id")
                _non_empty_string(call.get("name"), "payload.tool_calls[].name")
                if "args" not in call:
                    raise ItemSchemaError("tool_call payload 缺少 args")
        else:
            _non_empty_string(payload.get("tool_call_id"), "payload.tool_call_id")
            _non_empty_string(payload.get("name"), "payload.name")
            if "args" not in payload:
                raise ItemSchemaError("tool_call payload 缺少 args")
    if semantic_kind == SemanticKind.TOOL_RESULT:
        if payload_kind == PayloadKind.TEXT:
            # text 保持精确 Unicode 正文；identity 由 producer 显式写入 metadata。
            # 不从 item_id、invocation_id 或正文推测 call/result identity。
            identity = metadata
            identity_path = "metadata"
            outcome = None
        elif isinstance(payload, Mapping):
            identity = payload
            identity_path = "payload"
            outcome = payload.get("tool_outcome")
        else:
            raise ItemSchemaError("tool_result payload 必须是 object")
        _non_empty_string(identity.get("tool_call_id"), f"{identity_path}.tool_call_id")
        _non_empty_string(identity.get("result_id"), f"{identity_path}.result_id")
        if "execution_confirmed" in metadata and type(metadata["execution_confirmed"]) is not bool:
            raise ItemSchemaError("item-schema-incompatible: execution_confirmed 必须为 boolean")
        if outcome is not None and (not isinstance(outcome, str) or outcome not in _TOOL_OUTCOMES):
            raise ItemSchemaError("item-schema-incompatible: payload.tool_outcome 不是允许的 marker")
        if metadata.get("execution_confirmed") is False and outcome != "unknown":
            raise ItemSchemaError(
                "item-schema-incompatible: 未确认工具结果必须在 typed payload 标记 unknown；"
                "纯 text 无法承载 tool_outcome，必须选择 tool_result typed payload"
            )
        if outcome == "success":
            if status != CanonicalItemStatus.COMPLETED.value:
                raise ItemSchemaError(
                    "item-schema-incompatible: 只有 completed tool_result 才能标记 tool_outcome=success"
                )
            if metadata.get("execution_confirmed") is not True:
                raise ItemSchemaError(
                    "item-schema-incompatible: tool_outcome=success 必须带 execution_confirmed=true"
                )
        elif (
            outcome in {"failure", "cancelled"}
            and status != CanonicalItemStatus.COMPLETED.value
        ):
            raise ItemSchemaError(
                "item-schema-incompatible: 非 completed tool_result 只能省略 tool_outcome 或使用 unknown"
            )
    if semantic_kind == SemanticKind.ATTACHMENT:
        if not isinstance(payload, Mapping):
            raise ItemSchemaError("attachment_ref payload 必须是 object")
        _non_empty_string(payload.get("ref"), "payload.ref")
        if type(payload.get("length")) is not int or payload["length"] < 0:
            raise ItemSchemaError("payload.length 必须是非负整数")
        _non_empty_string(payload.get("hash"), "payload.hash")
    if semantic_kind == SemanticKind.COMPACTION_SUMMARY:
        if not isinstance(payload, Mapping):
            raise ItemSchemaError("compaction_summary payload 必须是 object")
        _non_empty_string(payload.get("summary_id"), "payload.summary_id")
        _non_empty_string(payload.get("view_revision"), "payload.view_revision")


@dataclass(frozen=True, slots=True)
class ProducerRef:
    producer_kind: str
    producer_id: str
    invocation_id: str | None = None
    source_version: str | None = None
    source_hash: str | None = None

    def __post_init__(self) -> None:
        validate_producer_ref(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "producer_kind": self.producer_kind,
            "producer_id": self.producer_id,
        }
        for name in ("invocation_id", "source_version", "source_hash"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


@dataclass(frozen=True, slots=True)
class CanonicalItemRecord:
    format_version: int
    record_type: str
    item_sequence: int
    item_id: str
    semantic_kind: str
    payload_kind: str
    status: str
    producer_ref: Mapping[str, object]
    payload: object
    content_hash: str
    created_at: str
    metadata: Mapping[str, object]
    turn_id: str | None = None
    turn_scope: str | None = None
    message_group_id: str | None = None
    wire_role: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def create(
        cls,
        *,
        item_sequence: int,
        item_id: str,
        semantic_kind: str | SemanticKind,
        payload_kind: str | PayloadKind,
        status: str | CanonicalItemStatus,
        producer_ref: Mapping[str, object] | ProducerRef,
        payload: object,
        created_at: str | None = None,
        metadata: Mapping[str, object] | None = None,
        turn_id: str | None = None,
        turn_scope: str | TurnScope | None = None,
        message_group_id: str | None = None,
        wire_role: str | None = None,
    ) -> CanonicalItemRecord:
        semantic_value = _non_empty_string(semantic_kind, "semantic_kind")
        payload_value = _non_empty_string(payload_kind, "payload_kind")
        status_value = _non_empty_string(status, "status")
        if created_at is None:
            created_at_value = datetime.now(UTC).isoformat()
        else:
            created_at_value = _non_empty_string(created_at, "created_at")
        turn_scope_value = (
            _non_empty_string(turn_scope, "turn_scope")
            if turn_scope is not None
            else None
        )
        return cls(
            format_version=2,
            record_type="item",
            item_sequence=item_sequence,
            item_id=item_id,
            semantic_kind=semantic_value,
            payload_kind=payload_value,
            status=status_value,
            producer_ref=(
                producer_ref.to_dict()
                if isinstance(producer_ref, ProducerRef)
                else producer_ref
            ),
            payload=payload,
            content_hash=content_hash(payload_value, payload),
            created_at=created_at_value,
            metadata=metadata if metadata is not None else {},
            turn_id=turn_id,
            turn_scope=turn_scope_value,
            message_group_id=message_group_id,
            wire_role=wire_role,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CanonicalItemRecord:
        missing = [field_name for field_name in _CORE_FIELDS if field_name not in value]
        if missing:
            raise ItemSchemaError(f"CanonicalItemRecord 缺少字段: {','.join(missing)}")
        forbidden = sorted(
            set(value) & {"id", "content", "message", "sequence", "role", "status_text"}
        )
        if forbidden:
            raise FormatDispatchError(
                "v2 CanonicalItemRecord 禁止 legacy message envelope 字段: "
                + ",".join(forbidden)
            )
        return cls(
            format_version=value["format_version"],
            record_type=value["record_type"],
            item_sequence=value["item_sequence"],
            item_id=value["item_id"],
            semantic_kind=value["semantic_kind"],
            payload_kind=value["payload_kind"],
            status=value["status"],
            producer_ref=value["producer_ref"],
            payload=value["payload"],
            content_hash=value["content_hash"],
            created_at=value["created_at"],
            metadata=value["metadata"],
            turn_id=value.get("turn_id"),
            turn_scope=value.get("turn_scope"),
            message_group_id=value.get("message_group_id"),
            wire_role=value.get("wire_role"),
        )

    def validate(self) -> None:
        if type(self.format_version) is not int or self.format_version != 2:
            raise FormatDispatchError("CanonicalItemRecord 必须是 v2 item envelope")
        if type(self.record_type) is not str or self.record_type != "item":
            raise FormatDispatchError("CanonicalItemRecord 必须是 v2 item envelope")
        if not isinstance(self.item_sequence, int) or isinstance(
            self.item_sequence, bool
        ):
            raise ItemSchemaError("item_sequence 必须是整数")
        if self.item_sequence <= 0:
            raise ItemSchemaError("item_sequence 必须大于 0")
        _non_empty_string(self.item_id, "item_id")
        semantic = _non_empty_string(self.semantic_kind, "semantic_kind")
        payload_type = _non_empty_string(self.payload_kind, "payload_kind")
        status = _non_empty_string(self.status, "status")
        validate_item_compatibility(semantic, payload_type, status)
        producer = _validate_producer(self.producer_ref)
        if not isinstance(self.metadata, Mapping):
            raise ItemSchemaError("metadata 必须是 object")
        _ensure_json_value(self.metadata, "metadata")
        if "tool_outcome" in self.metadata:
            raise ItemSchemaError("item-schema-incompatible: tool_outcome 只能位于 tool_result typed payload")
        _validate_payload(semantic, payload_type, status, self.payload, self.metadata)
        expected_hash = content_hash(payload_type, self.payload)
        if self.content_hash != expected_hash:
            raise ItemSchemaError("content_hash 与 payload 不一致")
        _non_empty_string(self.content_hash, "content_hash")
        _non_empty_string(self.created_at, "created_at")
        if self.turn_scope is not None and self.turn_scope not in {
            value.value for value in TurnScope
        }:
            raise ItemSchemaError(f"未知 turn_scope: {self.turn_scope}")
        for field_name in ("turn_id", "message_group_id", "wire_role"):
            field_value = getattr(self, field_name)
            if field_value is not None:
                _non_empty_string(field_value, field_name)
        if self.turn_scope == TurnScope.TURN_ROOT:
            if self.turn_id is None or semantic != SemanticKind.USER_INPUT:
                raise ItemSchemaError("turn_root 必须是带 turn_id 的 user_input")
        elif self.turn_scope == TurnScope.TURN_MEMBER:
            if self.turn_id is None:
                raise ItemSchemaError("turn_member 必须带 turn_id")
        elif (
            self.turn_scope in {TurnScope.AMBIENT, TurnScope.PENDING_NEXT_TURN}
            and self.turn_id is not None
        ):
            raise ItemSchemaError("ambient/pending_next_turn 不得带 turn_id")
        if self.turn_id is not None and self.turn_scope not in {
            TurnScope.TURN_ROOT,
            TurnScope.TURN_MEMBER,
        }:
            raise ItemSchemaError("带 turn_id 的 item 必须是 root/member")
        if semantic == SemanticKind.RUNTIME_NOTICE and self.turn_scope not in {
            TurnScope.AMBIENT,
            TurnScope.PENDING_NEXT_TURN,
        }:
            raise ItemSchemaError("runtime_notice 必须位于 ambient/pending_next_turn")
        if (
            semantic != SemanticKind.TOOL_RESULT
            and isinstance(self.payload, Mapping)
            and "tool_outcome" in self.payload
        ):
            raise ItemSchemaError("item-schema-incompatible: tool_outcome 只能出现在 tool_result payload")
        del producer

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "format_version": self.format_version,
            "record_type": self.record_type,
            "item_sequence": self.item_sequence,
            "item_id": self.item_id,
            "semantic_kind": self.semantic_kind,
            "payload_kind": self.payload_kind,
            "status": self.status,
            "producer_ref": dict(self.producer_ref),
            "payload": self.payload,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }
        for field_name in ("turn_id", "turn_scope", "message_group_id", "wire_role"):
            value = getattr(self, field_name)
            if value is not None:
                result[field_name] = value
        return result


__all__ = ["CanonicalItemRecord", "ProducerRef"]
