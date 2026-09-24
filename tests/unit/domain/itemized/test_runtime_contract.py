"""item part、producer 与 Turn/execution/model-call 的纯领域合同。"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.parts import ContentPart
from app.domain.itemized.records import ProducerRef
from app.domain.itemized.runtime import (
    ExecutionRecord,
    ModelCallRecord,
    ProvenanceEdge,
    TurnRecord,
)


@pytest.fixture
def turn_record() -> TurnRecord:
    return TurnRecord(
        turn_id="turn-1",
        thread_id="thread-1",
        turn_ordinal=1,
        source_branch_id="branch-1",
        root_input_item_id="root-1",
        accepted_ingress_id="ingress-1",
        acceptance_idempotency_key="acceptance-1",
        initial_execution_id="execution-1",
    )


@pytest.mark.parametrize("thread_id", [None, ""])
def test_turn_record_requires_non_empty_thread_identity(thread_id: object) -> None:
    # Turn 的 thread 归属是 (session_id, thread_id) 定位的一半；缺失或空串
    # 必须拒绝，不能回退到 session-only 的隐式 owner。
    with pytest.raises(TypeError, match="thread_id"):
        TurnRecord(
            turn_id="turn-1",
            turn_ordinal=1,
            source_branch_id="branch-1",
            root_input_item_id="root-1",
            accepted_ingress_id="ingress-1",
            acceptance_idempotency_key="acceptance-1",
            initial_execution_id="execution-1",
        )
    with pytest.raises(ItemSchemaError, match="thread_id"):
        TurnRecord(
            turn_id="turn-1",
            thread_id=thread_id,
            turn_ordinal=1,
            source_branch_id="branch-1",
            root_input_item_id="root-1",
            accepted_ingress_id="ingress-1",
            acceptance_idempotency_key="acceptance-1",
            initial_execution_id="execution-1",
        )


def test_turn_record_thread_identity_roundtrips(turn_record: TurnRecord) -> None:
    raw = json.loads(canonical_json_bytes(asdict(turn_record)))
    assert raw["thread_id"] == "thread-1"
    assert TurnRecord(**raw) == turn_record


@pytest.fixture
def control_records() -> tuple[ExecutionRecord, ModelCallRecord]:
    return (
        ExecutionRecord(execution_id="execution-1", turn_id="turn-1", attempt=1),
        ModelCallRecord(
            model_call_id="call-1",
            execution_id="execution-1",
            attempt=1,
            provider="provider-1",
        ),
    )


@pytest.mark.parametrize(
    "outcome",
    [
        "completed",
        "completed_empty",
        "failed",
        "interrupted",
        "cancelled",
        "execution_lost",
        "unknown",
    ],
)
def test_control_records_use_outcome_not_item_status(
    control_records: tuple[ExecutionRecord, ModelCallRecord], outcome: str
) -> None:
    for record in control_records:
        updated = replace(record, outcome=outcome)
        raw = json.loads(canonical_json_bytes(asdict(updated)))
        assert "status" not in raw and raw["outcome"] == outcome
        assert type(record)(**raw) == updated


@pytest.mark.parametrize(
    "outcome", ["partial", "incomplete", "draft", "empty", "running"]
)
def test_item_or_alias_status_is_not_a_control_outcome(
    control_records: tuple[ExecutionRecord, ModelCallRecord], outcome: str
) -> None:
    for record in control_records:
        with pytest.raises(ItemSchemaError):
            replace(record, outcome=outcome)


@pytest.mark.parametrize(
    "status",
    [
        "open",
        "active",
        "completed_empty",
        "interrupted",
        "cancelled",
        "failed",
        "unknown",
    ],
)
def test_non_completed_turn_has_no_final_item(
    turn_record: TurnRecord, status: str
) -> None:
    record = replace(turn_record, status=status)
    assert TurnRecord(**json.loads(canonical_json_bytes(asdict(record)))) == record
    with pytest.raises(ItemSchemaError, match="final_item_id"):
        replace(record, final_item_id="final-1")


def test_completed_turn_requires_final_and_keeps_acceptance_identity(
    turn_record: TurnRecord,
) -> None:
    with pytest.raises(ItemSchemaError, match="final_item_id"):
        replace(turn_record, status="completed")
    completed = replace(turn_record, status="completed", final_item_id="final-1")
    assert completed.acceptance_idempotency_key == "acceptance-1"
    assert completed.root_input_item_id == "root-1"
    with pytest.raises(FrozenInstanceError):
        completed.status = "active"


@pytest.mark.parametrize("field", ["producer_kind", "producer_id"])
def test_producer_identity_cannot_be_empty(field: str) -> None:
    with pytest.raises(ItemSchemaError):
        ProducerRef(
            **{"producer_kind": "provider", "producer_id": "provider-1", field: ""}
        )


@pytest.mark.parametrize(
    "relation",
    [
        "influenced_by",
        "transformed_by",
        "derived_from",
        "summary_of",
        "replaces",
        "notice_for",
        "causes",
        "result_of",
        "retry_of",
        "resumes",
        "replay_input",
    ],
)
def test_provenance_edges_keep_distinct_producer_and_relation_identity(
    relation: str,
) -> None:
    edge = ProvenanceEdge(
        edge_id="edge-1", relation=relation, source_ref="source-1", target_ref="item-1"
    )
    assert ProvenanceEdge(**json.loads(canonical_json_bytes(asdict(edge)))) == edge
    with pytest.raises(ItemSchemaError):
        replace(edge, source_ref="")


def test_content_part_uses_the_parent_payload_value_for_hash(
    hash_vectors: dict[str, object],
) -> None:
    content = hash_vectors["scenario"]["item"]["payload"]
    part = ContentPart.create(
        part_id="part-1",
        part_ordinal=0,
        part_semantic_kind="assistant_output",
        content=content,
    )
    assert part.part_ordinal == 0
    assert ContentPart(**json.loads(canonical_json_bytes(part.to_dict()))) == part
    with pytest.raises(ItemSchemaError, match="hash"):
        replace(part, content={"text": "changed"})
