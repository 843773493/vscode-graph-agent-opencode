"""canonical item 的投影、来源与关系值对象；不解释自由扩展。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.schema import PROTECTIONS, VISIBILITIES
from app.domain.itemized.validation import _non_empty_string


def _object(value: object, allowed: set[str], required: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) - allowed or required - set(value):
        raise ItemSchemaError(f"{name} 字段缺失或含未知字段")
    return value


def _ordinal(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ItemSchemaError(f"{name} 必须是非负整数")


def _strings(value: object, names: tuple[str, ...], prefix: str) -> None:
    for name in names:
        child = getattr(value, name)
        if child is not None:
            _non_empty_string(child, f"{prefix}.{name}")


@dataclass(frozen=True, slots=True)
class ProjectionGroup:
    # 同一 message_group_id 的连续组内序号与总数；单 item 不创建 group。
    ordinal: int
    size: int
    # 原始 carrier 的物理形态，禁止按正文或邻接推断。
    content_form: str

    def __post_init__(self) -> None:
        _ordinal(self.ordinal, "ProjectionGroup.ordinal")
        if type(self.size) is not int or self.size < 2 or self.ordinal >= self.size:
            raise ItemSchemaError("ProjectionGroup size/ordinal 非法")
        if not isinstance(self.content_form, str) or self.content_form not in {"str", "list"}:
            raise ItemSchemaError("ProjectionGroup.content_form 必须是 str|list")

    @classmethod
    def from_dict(cls, value: object) -> ProjectionGroup:
        return cls(**_object(value, {"ordinal", "size", "content_form"}, {"ordinal", "size", "content_form"}, "ProjectionGroup"))

    def to_dict(self) -> dict[str, object]:
        return {"ordinal": self.ordinal, "size": self.size, "content_form": self.content_form}


@dataclass(frozen=True, slots=True)
class ProjectionPartRef:
    # Provider 已声明的 part 身份与原始有序 block 坐标，不得用正文生成。
    id: str
    index: int
    type: str | None = None

    def __post_init__(self) -> None:
        _non_empty_string(self.id, "ProjectionPartRef.id")
        _ordinal(self.index, "ProjectionPartRef.index")
        if self.type is not None:
            _non_empty_string(self.type, "ProjectionPartRef.type")

    @classmethod
    def from_dict(cls, value: object) -> ProjectionPartRef:
        return cls(**_object(value, {"id", "index", "type"}, {"id", "index"}, "ProjectionPartRef"))

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "index": self.index, "type": self.type}


@dataclass(frozen=True, slots=True)
class ReasoningCarrier:
    # 仅允许可读文本 carrier；签名/受保护正文必须使用 typed opaque payload。
    type: str
    text_key: str
    id: str | None = None
    index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or self.type not in {"reasoning", "reasoning_content", "reasoning_items", "thinking", "redacted_thinking"}:
            raise ItemSchemaError("未知 ReasoningCarrier.type")
        if not isinstance(self.text_key, str) or self.text_key not in {"reasoning", "reasoning_content", "text", "thinking"}:
            raise ItemSchemaError("未知 ReasoningCarrier.text_key")
        if self.id is not None:
            _non_empty_string(self.id, "ReasoningCarrier.id")
        if self.index is not None:
            _ordinal(self.index, "ReasoningCarrier.index")

    @classmethod
    def from_dict(cls, value: object) -> ReasoningCarrier:
        return cls(**_object(value, {"type", "text_key", "id", "index"}, {"type", "text_key"}, "ReasoningCarrier"))

    def to_dict(self) -> dict[str, object]:
        return {entry.name: child for entry in fields(self) if (child := getattr(self, entry.name)) is not None}


@dataclass(frozen=True, slots=True)
class ItemProjectionIdentity:
    # checkpoint message 与真实 model-call 身份分别可空，不能从 item ID 回填。
    projection_message_id: str | None = None
    model_call_id: str | None = None
    # stream block 必须同时携带 model-call scope 与显式 block index。
    block_id: str | None = None
    block_index: int | None = None
    # 最终 carrier 引用的有序 part；重复坐标不可用来去重或替换。
    content_part_refs: tuple[ProjectionPartRef, ...] = ()
    reasoning_carrier: ReasoningCarrier | None = None
    # Provider 声明的消息阶段，供已有展示投影使用；不决定 Turn final pointer。
    phase: str | None = None
    completion_reason: str | None = None

    def __post_init__(self) -> None:
        _strings(self, ("projection_message_id", "model_call_id", "block_id", "phase", "completion_reason"), "ItemProjectionIdentity")
        if self.block_id is not None:
            if self.model_call_id is None or self.block_index is None:
                raise ItemSchemaError("stream block 缺少 typed model_call_id/block_index")
        elif self.block_index is not None:
            raise ItemSchemaError("block_index 不得脱离 block_id")
        if self.block_index is not None:
            _ordinal(self.block_index, "ItemProjectionIdentity.block_index")
        if not isinstance(self.content_part_refs, tuple) or not all(isinstance(ref, ProjectionPartRef) for ref in self.content_part_refs):
            raise ItemSchemaError("content_part_refs 必须是有序 typed ProjectionPartRef tuple")
        indices = [ref.index for ref in self.content_part_refs]
        if indices != sorted(set(indices)):
            raise ItemSchemaError("content_part_refs index 必须有序且唯一")
        if self.reasoning_carrier is not None and not isinstance(self.reasoning_carrier, ReasoningCarrier):
            raise ItemSchemaError("reasoning_carrier 必须是 typed ReasoningCarrier")

    @classmethod
    def from_dict(cls, value: object) -> ItemProjectionIdentity:
        allowed = {entry.name for entry in fields(cls)}
        raw = dict(_object(value, allowed, set(), "ItemProjectionIdentity"))
        refs = raw.pop("content_part_refs", [])
        if not isinstance(refs, list):
            raise ItemSchemaError("content_part_refs 必须是 array")
        carrier = raw.pop("reasoning_carrier", None)
        return cls(**raw, content_part_refs=tuple(ProjectionPartRef.from_dict(ref) for ref in refs), reasoning_carrier=ReasoningCarrier.from_dict(carrier) if carrier is not None else None)

    def to_dict(self) -> dict[str, object]:
        result = {entry.name: child for entry in fields(self) if (child := getattr(self, entry.name)) is not None and entry.name not in {"content_part_refs", "reasoning_carrier"}}
        result["content_part_refs"] = [ref.to_dict() for ref in self.content_part_refs]
        if self.reasoning_carrier is not None:
            result["reasoning_carrier"] = self.reasoning_carrier.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class ToolResultEvidence:
    # 纯文本结果必须独立保存调用/结果身份和真实执行确认，不能从正文推断。
    tool_call_id: str
    result_id: str
    execution_confirmed: bool

    def __post_init__(self) -> None:
        _strings(self, ("tool_call_id", "result_id"), "ToolResultEvidence")
        if self.tool_call_id is None or self.result_id is None or type(self.execution_confirmed) is not bool:
            raise ItemSchemaError("ToolResultEvidence 缺少真实身份或 boolean execution_confirmed")

    @classmethod
    def from_dict(cls, value: object) -> ToolResultEvidence:
        names = {"tool_call_id", "result_id", "execution_confirmed"}
        return cls(**_object(value, names, names, "ToolResultEvidence"))

    def to_dict(self) -> dict[str, object]:
        return {"tool_call_id": self.tool_call_id, "result_id": self.result_id, "execution_confirmed": self.execution_confirmed}


@dataclass(frozen=True, slots=True)
class ItemSourceManifest:
    # canonical source 已发布的语义 revision；不读当前源、不复制造成者身份。
    source_revision: str
    # 投影可见性与正文保护等级必须显式存在，不从 extension 判断。
    visibility: str
    protection: str

    def __post_init__(self) -> None:
        _non_empty_string(self.source_revision, "ItemSourceManifest.source_revision")
        if not isinstance(self.visibility, str) or self.visibility not in VISIBILITIES or not isinstance(self.protection, str) or self.protection not in PROTECTIONS:
            raise ItemSchemaError("ItemSourceManifest visibility/protection 非法")

    @classmethod
    def from_dict(cls, value: object) -> ItemSourceManifest:
        names = {"source_revision", "visibility", "protection"}
        return cls(**_object(value, names, names, "ItemSourceManifest"))

    def to_dict(self) -> dict[str, object]:
        return {"source_revision": self.source_revision, "visibility": self.visibility, "protection": self.protection}


@dataclass(frozen=True, slots=True)
class ItemProvenanceRelation:
    # 身份替换与 fork/import 来源是 typed 关系；目标不是可执行定位器。
    relation: str
    target_kind: str
    target_id: str
    target_session_id: str | None = None
    target_thread_id: str | None = None
    target_turn_id: str | None = None
    target_model_call_id: str | None = None
    target_message_sequence: int | None = None
    target_part_id: str | None = None
    target_legacy_item_id: str | None = None

    def __post_init__(self) -> None:
        _strings(self, ("target_id", "target_session_id", "target_thread_id", "target_turn_id", "target_model_call_id", "target_part_id", "target_legacy_item_id"), "ItemProvenanceRelation")
        if self.target_id is None or not isinstance(self.relation, str) or self.relation not in {"replaces", "derived_from"}:
            raise ItemSchemaError("ItemProvenanceRelation relation/target 非法")
        if not isinstance(self.target_kind, str) or self.target_kind not in {"projection_message", "canonical_item", "legacy_message"}:
            raise ItemSchemaError("未知 ItemProvenanceRelation.target_kind")
        if self.target_kind == "canonical_item" and (self.target_session_id is None or self.target_thread_id is None):
            raise ItemSchemaError("canonical 来源关系缺少完整 owner thread")
        if self.target_message_sequence is not None:
            _ordinal(self.target_message_sequence, "ItemProvenanceRelation.target_message_sequence")
        if self.target_kind != "legacy_message" and (self.target_message_sequence is not None or self.target_part_id is not None or self.target_legacy_item_id is not None):
            raise ItemSchemaError("非 legacy 关系不得携带 v1 coordinate")

    @classmethod
    def from_dict(cls, value: object) -> ItemProvenanceRelation:
        names = {entry.name for entry in fields(cls)}
        return cls(**_object(value, names, {"relation", "target_kind", "target_id"}, "ItemProvenanceRelation"))

    def to_dict(self) -> dict[str, object]:
        return {entry.name: child for entry in fields(self) if (child := getattr(self, entry.name)) is not None}
