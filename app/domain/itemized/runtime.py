"""v2 Turn、execution、model-call、provenance 与 item draft 领域定义。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.domain.itemized.enums import ControlOutcome, TurnStatus
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
    ItemSourceManifest,
    ProjectionGroup,
    ToolResultEvidence,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.schema import ITEM_STATUSES as _ITEM_STATUSES
from app.domain.itemized.schema import PROTECTIONS as _PROTECTIONS
from app.domain.itemized.schema import PROVENANCE_RELATIONS as _PROVENANCE_RELATIONS
from app.domain.itemized.schema import VISIBILITIES as _VISIBILITIES
from app.domain.itemized.validation import _non_empty_string


@dataclass(frozen=True, slots=True)
class TurnRecord:
    turn_id: str
    # thread_id 是 (session_id, thread_id) 定位的一半：Turn 归属的真实
    # thread，不能由 turn_id 或 branch 反推；同一 session 下的 sibling
    # thread 各自拥有独立 Turn 序列。
    thread_id: str
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
            "thread_id",
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
    """一次 execution 的结果；unknown 只表示该 execution 的 outcome 尚未确定。

    unknown 不能用来判断 Provider 请求是否已发送；发送状态由 ModelCall 的独立
    dispatch_state 表示。execution 可在多次模型调用及工具之后结束，不能从某次调用终态推导结果。
    """

    session_id: str
    thread_id: str
    execution_id: str
    # 用户 execution 关联真实 Turn；内部 execution 与 SQLite admission 一致为 None。
    turn_id: str | None
    attempt: int
    outcome: str = ControlOutcome.UNKNOWN
    resumed_from_execution_id: str | None = None

    def __post_init__(self) -> None:
        _non_empty_string(self.session_id, "session_id")
        _non_empty_string(self.thread_id, "thread_id")
        _non_empty_string(self.execution_id, "execution_id")
        if self.turn_id is not None:
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
    """一次 Provider 模型调用的结果；unknown 只表示该调用的 outcome 尚未确定。

    unknown 不能用来判断 Provider 请求是否已发送；dispatch_state 独立记录请求发送状态。
    调用完成可先于工具和 execution 完成，不能由 execution 终态改写调用事实。
    """

    session_id: str
    thread_id: str
    model_call_id: str
    execution_id: str
    attempt: int
    provider: str
    outcome: str = ControlOutcome.UNKNOWN
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "thread_id",
            "model_call_id",
            "execution_id",
            "provider",
        ):
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
class TurnExecutionLink:
    """在同一 SessionThread 内把 Turn 与 execution 绑定。"""

    session_id: str
    thread_id: str
    turn_id: str
    execution_id: str
    execution_role: str
    execution_ordinal: int
    link_idempotency_key: str

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "thread_id",
            "turn_id",
            "execution_id",
            "execution_role",
            "link_idempotency_key",
        ):
            _non_empty_string(getattr(self, name), name)
        if (
            not isinstance(self.execution_ordinal, int)
            or isinstance(self.execution_ordinal, bool)
            or self.execution_ordinal <= 0
        ):
            raise ItemSchemaError("TurnExecutionLink.execution_ordinal 必须是正整数")


@dataclass(frozen=True, slots=True)
class ProvenanceEdge:
    edge_id: str
    relation: str
    source_ref: str
    target_ref: str
    edge_idempotency_key: str | None = None
    attempt: int | None = None
    supersedes_edge_id: str | None = None
    replay_input: bool = False
    produced_order: int | None = None
    visibility: str = "internal"
    protection: str = "public"
    detail_ref: DetailRef | None = None

    def __post_init__(self) -> None:
        for name in ("edge_id", "source_ref", "target_ref"):
            _non_empty_string(getattr(self, name), name)
        _non_empty_string(self.relation, "relation")
        if self.relation not in _PROVENANCE_RELATIONS:
            raise ItemSchemaError(f"未知 provenance relation: {self.relation}")
        if self.edge_idempotency_key is None:
            object.__setattr__(self, "edge_idempotency_key", self.edge_id)
        else:
            _non_empty_string(self.edge_idempotency_key, "edge_idempotency_key")
        if self.attempt is not None and (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or self.attempt <= 0
        ):
            raise ItemSchemaError("provenance attempt 必须是正整数")
        if self.produced_order is not None and (
            not isinstance(self.produced_order, int)
            or isinstance(self.produced_order, bool)
            or self.produced_order < 0
        ):
            raise ItemSchemaError("provenance produced_order 必须是非负整数")
        if self.supersedes_edge_id is not None:
            _non_empty_string(self.supersedes_edge_id, "supersedes_edge_id")
        if type(self.replay_input) is not bool:
            raise ItemSchemaError("provenance replay_input 必须是 boolean")
        if self.visibility not in _VISIBILITIES:
            raise ItemSchemaError(f"未知 provenance visibility: {self.visibility}")
        if self.protection not in _PROTECTIONS:
            raise ItemSchemaError(f"未知 provenance protection: {self.protection}")
        if self.detail_ref is not None and not isinstance(self.detail_ref, DetailRef):
            raise ItemSchemaError("provenance detail_ref 必须是 typed DetailRef")


@dataclass(slots=True)
class ItemDraft:
    item_id: str
    semantic_kind: str
    payload_kind: str
    producer_ref: Mapping[str, object]
    payload: object
    extensions: dict[str, object] = field(default_factory=dict)
    projection_group: ProjectionGroup | None = None
    projection_identity: ItemProjectionIdentity | None = None
    tool_result_evidence: ToolResultEvidence | None = None
    source_manifest: ItemSourceManifest | None = None
    provenance_relations: tuple[ItemProvenanceRelation, ...] = ()
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
            extensions=self.extensions,
            projection_group=self.projection_group,
            projection_identity=self.projection_identity,
            tool_result_evidence=self.tool_result_evidence,
            source_manifest=self.source_manifest,
            provenance_relations=self.provenance_relations,
            turn_id=self.turn_id,
            turn_scope=self.turn_scope,
            message_group_id=self.message_group_id,
            wire_role=self.wire_role,
        )

__all__ = [
    "ExecutionRecord",
    "ItemDraft",
    "ModelCallRecord",
    "ProvenanceEdge",
    "TurnExecutionLink",
    "TurnRecord",
]
