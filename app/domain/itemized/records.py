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
    canonical_json_bytes,
    content_hash,
)
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
    ItemSourceManifest,
    ProjectionGroup,
    ToolResultEvidence,
)
from app.domain.itemized.schema import (
    CORE_FIELDS as _CORE_FIELDS,
)
from app.domain.itemized.schema import (
    TOOL_OUTCOMES as _TOOL_OUTCOMES,
)
from app.domain.itemized.schema import (
    validate_extension_payload,
    validate_extensions,
    validate_item_compatibility,
    validate_producer_ref,
)
from app.domain.itemized.validation import _non_empty_string

_OPTIONAL_FIELDS = frozenset(
    {"turn_id", "turn_scope", "message_group_id", "wire_role", "projection_group", "projection_identity", "tool_result_evidence", "source_manifest", "provenance_relations"}
)
_INTERNAL_DISPLAY_KINDS = frozenset({"delegated_task", "generated_session_result"})
_RUNTIME_NOTICE_PAYLOAD_FIELDS = frozenset({"prompt", "display_part"})
_DISPLAY_PART_FIELDS = frozenset({"kind", "text", "provenance"})
_DISPLAY_PROVENANCE_FIELDS = frozenset(
    {
        "source_session_id",
        "source_thread_id",
        "delegation_id",
        "source_execution_id",
        "source_item_id",
    }
)
GENERATED_RESULT_ANNOTATION_FIELDS = frozenset({
    "structured_prompt_kind", "structured_prompt_schema_version", "boxteam_session_provider_id",
})


def validate_generated_result_extensions(extensions: Mapping[str, object]) -> None:
    """生成结果的扩展只承载已知结构标记，来源由闭合 payload 独占。"""
    validate_extensions(extensions)
    annotation_schema = "boxteam.checkpoint.annotations"
    unknown = set(extensions) - {annotation_schema}
    envelope = extensions.get(annotation_schema)
    if envelope is not None:
        if envelope["extension_version"] != "1" or not isinstance(envelope["value"], Mapping):
            raise ItemSchemaError("generated_session_result annotations 必须使用已知的闭合版本")
        unknown.update(set(envelope["value"]) - GENERATED_RESULT_ANNOTATION_FIELDS)
    if unknown:
        raise ItemSchemaError("generated_session_result extensions 含未知或控制字段: " + ",".join(sorted(unknown)))


def _strict_mapping(value: object, allowed: frozenset[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ItemSchemaError(f"{name} 必须是 object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ItemSchemaError(f"{name} 含未知字段: " + ",".join(unknown))
    return value



def user_input_payload(content: object, attachments: object) -> object:
    """用户正文与安全附件坐标属于 payload，不能靠扩展恢复业务输入。"""
    if not isinstance(attachments, list):
        return content
    safe = []
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            continue
        file_id = attachment.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            continue
        record = {"file_id": file_id}
        for key in ("name", "content_type", "path", "preview_status"):
            child = attachment.get(key)
            if isinstance(child, str) and child:
                record[key] = child
        safe.append(record)
    return {"content": content, "attachments": safe} if safe else content


@dataclass(frozen=True, slots=True)
class InternalDisplayProvenance:
    source_session_id: str | None = None
    source_thread_id: str | None = None
    delegation_id: str | None = None
    source_execution_id: str | None = None
    source_item_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in _DISPLAY_PROVENANCE_FIELDS:
            value = getattr(self, field_name)
            if value is not None:
                _non_empty_string(value, f"display_part.provenance.{field_name}")

    @classmethod
    def from_dict(cls, value: object) -> InternalDisplayProvenance:
        raw = _strict_mapping(value, _DISPLAY_PROVENANCE_FIELDS, "display_part.provenance")
        return cls(
            **{
                field_name: (
                    _non_empty_string(
                        raw[field_name], f"display_part.provenance.{field_name}"
                    )
                    if field_name in raw
                    else None
                )
                for field_name in _DISPLAY_PROVENANCE_FIELDS
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            field_name: value
            for field_name in _DISPLAY_PROVENANCE_FIELDS
            if (value := getattr(self, field_name)) is not None
        }


@dataclass(frozen=True, slots=True)
class InternalDisplayPart:
    kind: str
    text: str
    provenance: InternalDisplayProvenance

    def __post_init__(self) -> None:
        if self.kind not in _INTERNAL_DISPLAY_KINDS:
            raise ItemSchemaError(f"未知 display_part.kind: {self.kind}")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ItemSchemaError("display_part.text 必须是非空字符串")
        if not isinstance(self.provenance, InternalDisplayProvenance):
            raise ItemSchemaError("display_part.provenance 必须是 typed provenance")

    @classmethod
    def from_dict(cls, value: object) -> InternalDisplayPart:
        raw = _strict_mapping(value, _DISPLAY_PART_FIELDS, "display_part")
        required = _DISPLAY_PART_FIELDS
        missing = sorted(required - set(raw))
        if missing:
            raise ItemSchemaError("display_part 缺少字段: " + ",".join(missing))
        kind = _non_empty_string(raw["kind"], "display_part.kind")
        text = raw["text"]
        if not isinstance(text, str):
            raise ItemSchemaError("display_part.text 必须是字符串")
        return cls(
            kind=kind,
            text=text,
            provenance=InternalDisplayProvenance.from_dict(raw["provenance"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "text": self.text,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RuntimeNoticePayload:
    prompt: str
    display_part: InternalDisplayPart

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ItemSchemaError("runtime_notice.prompt 必须是非空字符串")
        if not isinstance(self.display_part, InternalDisplayPart):
            raise ItemSchemaError("runtime_notice.display_part 必须是 typed display part")

    @classmethod
    def from_dict(cls, value: object) -> RuntimeNoticePayload:
        raw = _strict_mapping(
            value, _RUNTIME_NOTICE_PAYLOAD_FIELDS, "RuntimeNoticePayload"
        )
        missing = sorted(_RUNTIME_NOTICE_PAYLOAD_FIELDS - set(raw))
        if missing:
            raise ItemSchemaError("RuntimeNoticePayload 缺少字段: " + ",".join(missing))
        prompt = raw["prompt"]
        if not isinstance(prompt, str):
            raise ItemSchemaError("runtime_notice.prompt 必须是字符串")
        return cls(
            prompt=prompt,
            display_part=InternalDisplayPart.from_dict(raw["display_part"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "prompt": self.prompt,
            "display_part": self.display_part.to_dict(),
        }


def _validate_producer(value: object) -> dict[str, object]:
    return validate_producer_ref(value)


def _validate_payload(
    semantic_kind: str,
    payload_kind: str,
    status: str,
    payload: object,
    tool_result_evidence: ToolResultEvidence | None,
    extensions: Mapping[str, object],
) -> None:
    _ensure_json_value(payload)
    if payload_kind == PayloadKind.TEXT and not isinstance(payload, str):
        raise ItemSchemaError("text payload 必须是字符串")
    if payload_kind == PayloadKind.STRUCTURED_CONTENT and not isinstance(
        payload, (Mapping, list, tuple)
    ):
        raise ItemSchemaError("structured_content payload 必须是 object 或 array")
    if (
        semantic_kind == SemanticKind.RUNTIME_NOTICE
        and payload_kind == PayloadKind.STRUCTURED_CONTENT
        and isinstance(payload, Mapping)
        and set(payload) & _RUNTIME_NOTICE_PAYLOAD_FIELDS
    ):
        notice = RuntimeNoticePayload.from_dict(payload)
        if notice.display_part.kind == "generated_session_result":
            validate_generated_result_extensions(extensions)
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
            if tool_result_evidence is None:
                raise ItemSchemaError("text tool_result 缺少 typed ToolResultEvidence")
            outcome = None
            confirmed = tool_result_evidence.execution_confirmed
            if not confirmed:
                raise ItemSchemaError("纯 text 无法承载 tool_outcome=unknown；未确认结果必须使用结构化 payload")
        elif isinstance(payload, Mapping):
            if tool_result_evidence is not None:
                raise ItemSchemaError("结构化 tool_result 不得同时携带文本结果 evidence")
            _non_empty_string(payload.get("tool_call_id"), "payload.tool_call_id")
            _non_empty_string(payload.get("result_id"), "payload.result_id")
            confirmed = payload.get("execution_confirmed")
            if type(confirmed) is not bool:
                raise ItemSchemaError("payload.execution_confirmed 必须是 boolean")
            outcome = payload.get("tool_outcome")
        else:
            raise ItemSchemaError("tool_result payload 必须是 object 或带 evidence 的 text")
        if outcome is not None and (not isinstance(outcome, str) or outcome not in _TOOL_OUTCOMES):
            raise ItemSchemaError("item-schema-incompatible: payload.tool_outcome 不是允许的 marker")
        if payload_kind != PayloadKind.TEXT and not confirmed and outcome != "unknown":
            raise ItemSchemaError("item-schema-incompatible: 未确认结果必须显式标记 tool_outcome=unknown")
        if outcome == "success" and (
            status != CanonicalItemStatus.COMPLETED.value or not confirmed
        ):
            raise ItemSchemaError("item-schema-incompatible: success 必须是 completed 且 execution_confirmed=true")
        if outcome in {"failure", "cancelled"} and status != CanonicalItemStatus.COMPLETED.value:
            raise ItemSchemaError("item-schema-incompatible: 非 completed 结果只能省略 outcome 或使用 unknown")
    elif tool_result_evidence is not None:
        raise ItemSchemaError("ToolResultEvidence 只能用于纯文本 tool_result")
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


def parse_item_projection_manifest(value: object) -> dict[str, object]:
    """恢复 catalogue 的无正文镜像；禁止扩展覆盖 typed owner。"""
    fields = {"projection_group", "projection_identity", "tool_result_evidence", "source_manifest", "provenance_relations", "extensions"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ItemSchemaError("item projection manifest 字段缺失或含未知字段")
    validate_extensions(value["extensions"])
    types = {
        "projection_group": ProjectionGroup,
        "projection_identity": ItemProjectionIdentity,
        "tool_result_evidence": ToolResultEvidence,
        "source_manifest": ItemSourceManifest,
    }
    result = {"extensions": dict(value["extensions"])}
    for name, target_type in types.items():
        child = value[name]
        result[name] = target_type.from_dict(child).to_dict() if child is not None else None
    relations = value["provenance_relations"]
    if not isinstance(relations, list):
        raise ItemSchemaError("item provenance_relations 必须是有序 array")
    result["provenance_relations"] = [ItemProvenanceRelation.from_dict(child).to_dict() for child in relations]
    return result


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
    # 未知扩展保留版本与 value；禁止由它控制归属、去重、工具确认或恢复。
    extensions: Mapping[str, object]
    turn_id: str | None = None
    turn_scope: str | None = None
    message_group_id: str | None = None
    wire_role: str | None = None
    # 仅显式拆分的 carrier 成组；单 item 为 None，不从邻接猜测 group。
    projection_group: ProjectionGroup | None = None
    # checkpoint/stream 身份可空；不可从 item ID、正文或 extension 补造。
    projection_identity: ItemProjectionIdentity | None = None
    # 纯文本工具结果必须有 evidence，结构化结果由 payload 独占。
    tool_result_evidence: ToolResultEvidence | None = None
    # 语义 source revision/可见性/保护可空；不存在时使用 canonical 自身身份。
    source_manifest: ItemSourceManifest | None = None
    # 已提交替换与导入/fork 来源；item_relations 只镜像这份 typed 关系。
    provenance_relations: tuple[ItemProvenanceRelation, ...] = ()

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
        extensions: Mapping[str, object] | None = None,
        projection_group: ProjectionGroup | None = None,
        projection_identity: ItemProjectionIdentity | None = None,
        tool_result_evidence: ToolResultEvidence | None = None,
        source_manifest: ItemSourceManifest | None = None,
        provenance_relations: tuple[ItemProvenanceRelation, ...] = (),
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
            extensions=extensions if extensions is not None else {},
            projection_group=projection_group,
            projection_identity=projection_identity,
            tool_result_evidence=tool_result_evidence,
            source_manifest=source_manifest,
            provenance_relations=provenance_relations,
            turn_id=turn_id,
            turn_scope=turn_scope_value,
            message_group_id=message_group_id,
            wire_role=wire_role,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CanonicalItemRecord:
        if not isinstance(value, Mapping):
            raise ItemSchemaError("CanonicalItemRecord 必须是 object")
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
        unknown = sorted(set(value) - set(_CORE_FIELDS) - _OPTIONAL_FIELDS)
        if unknown:
            raise ItemSchemaError(
                "CanonicalItemRecord 含未知字段: " + ",".join(unknown)
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
            extensions=value["extensions"],
            projection_group=ProjectionGroup.from_dict(value["projection_group"]) if value.get("projection_group") is not None else None,
            projection_identity=ItemProjectionIdentity.from_dict(value["projection_identity"]) if value.get("projection_identity") is not None else None,
            tool_result_evidence=ToolResultEvidence.from_dict(value["tool_result_evidence"]) if value.get("tool_result_evidence") is not None else None,
            source_manifest=ItemSourceManifest.from_dict(value["source_manifest"]) if value.get("source_manifest") is not None else None,
            provenance_relations=cls._parse_relations(value.get("provenance_relations", [])),
            turn_id=value.get("turn_id"),
            turn_scope=value.get("turn_scope"),
            message_group_id=value.get("message_group_id"),
            wire_role=value.get("wire_role"),
        )

    @staticmethod
    def _parse_relations(value: object) -> tuple[ItemProvenanceRelation, ...]:
        if not isinstance(value, list):
            raise ItemSchemaError("provenance_relations 必须是有序 array")
        return tuple(ItemProvenanceRelation.from_dict(entry) for entry in value)

    @property
    def tool_call_id(self) -> str | None:
        """派生目录索引；真实身份只来自 typed evidence 或工具 payload。"""
        if self.tool_result_evidence is not None:
            return self.tool_result_evidence.tool_call_id
        if self.semantic_kind not in {SemanticKind.TOOL_CALL, SemanticKind.TOOL_RESULT} or not isinstance(self.payload, Mapping):
            return None
        call_id = self.payload.get("tool_call_id")
        if isinstance(call_id, str):
            return call_id
        calls = self.payload.get("tool_calls")
        if isinstance(calls, list) and len(calls) == 1:
            return calls[0]["id"]
        return None

    def projection_manifest(self) -> dict[str, object]:
        """SQLite 只保存无正文 typed 投影/关系镜像，由 JSONL 逐字段核对。"""
        return {
            "projection_group": self.projection_group.to_dict() if self.projection_group is not None else None,
            "projection_identity": self.projection_identity.to_dict() if self.projection_identity is not None else None,
            "tool_result_evidence": self.tool_result_evidence.to_dict() if self.tool_result_evidence is not None else None,
            "source_manifest": self.source_manifest.to_dict() if self.source_manifest is not None else None,
            "provenance_relations": [entry.to_dict() for entry in self.provenance_relations],
            "extensions": dict(self.extensions),
        }

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
        validate_extensions(self.extensions)
        for name, expected_type in (
            ("projection_group", ProjectionGroup),
            ("projection_identity", ItemProjectionIdentity),
            ("tool_result_evidence", ToolResultEvidence),
            ("source_manifest", ItemSourceManifest),
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, expected_type):
                raise ItemSchemaError(f"{name} 必须是 typed {expected_type.__name__}")
        if self.projection_group is not None and self.message_group_id is None:
            raise ItemSchemaError("projection_group 必须绑定显式 message_group_id")
        if not isinstance(self.provenance_relations, tuple) or not all(
            isinstance(entry, ItemProvenanceRelation) for entry in self.provenance_relations
        ):
            raise ItemSchemaError("provenance_relations 必须是 typed tuple")
        relation_keys = [canonical_json_bytes(entry.to_dict()) for entry in self.provenance_relations]
        if len(relation_keys) != len(set(relation_keys)):
            raise ItemSchemaError("provenance_relations 不得重复")
        _validate_payload(semantic, payload_type, status, self.payload, self.tool_result_evidence, self.extensions)
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
            if producer.get("producer_kind") != "user":
                raise ItemSchemaError(
                    "turn_root 的 producer_ref.producer_kind 必须是 user"
                )
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
            "extensions": dict(self.extensions),
        }
        for field_name in ("turn_id", "turn_scope", "message_group_id", "wire_role"):
            value = getattr(self, field_name)
            if value is not None:
                result[field_name] = value
        for name in ("projection_group", "projection_identity", "tool_result_evidence", "source_manifest"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value.to_dict()
        if self.provenance_relations:
            result["provenance_relations"] = [entry.to_dict() for entry in self.provenance_relations]
        return result


__all__ = [
    "CanonicalItemRecord",
    "InternalDisplayPart",
    "InternalDisplayProvenance",
    "ItemProjectionIdentity",
    "ItemProvenanceRelation",
    "ItemSourceManifest",
    "ProducerRef",
    "ProjectionGroup",
    "RuntimeNoticePayload",
    "ToolResultEvidence",
]
