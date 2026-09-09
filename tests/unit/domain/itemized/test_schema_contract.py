"""itemized schema 的闭合集合、JCS 和 provenance 合同。"""

from __future__ import annotations

import math
from dataclasses import asdict

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes, validate_hash_token
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.runtime import ProvenanceEdge
from app.domain.itemized.schema import (
    ALLOWED_PAYLOADS,
    ALLOWED_STATUSES,
    PRODUCER_KINDS,
    PROVENANCE_RELATIONS,
    SELECTION_COMPATIBILITY,
    validate_item_compatibility,
    validate_producer_ref,
    validate_selection_compatibility,
)
from app.domain.itemized.selection import ContextSelectionEntry


def test_production_compatibility_matrix_is_closed() -> None:
    expected_payloads = {
        "user_input": {"text", "structured_content"},
        "assistant_output": {"text", "structured_content"},
        "reasoning": {"text", "summary", "opaque", "extension"},
        "tool_call": {"tool_call", "structured_content"},
        "tool_result": {
            "text",
            "structured_content",
            "tool_result",
            "opaque",
            "extension",
        },
        "runtime_notice": {"text", "structured_content", "opaque", "extension"},
        "compaction_summary": {"summary", "structured_content"},
        "attachment": {"attachment_ref"},
        "extension": {"extension", "opaque"},
    }
    expected_statuses = {
        "user_input": {"completed"},
        "assistant_output": {
            "completed",
            "partial",
            "incomplete",
            "cancelled",
            "failed",
            "unknown",
        },
        "reasoning": {
            "completed",
            "partial",
            "incomplete",
            "cancelled",
            "failed",
            "unknown",
        },
        "tool_call": {
            "completed",
            "partial",
            "incomplete",
            "cancelled",
            "failed",
            "unknown",
        },
        "tool_result": {
            "completed",
            "partial",
            "incomplete",
            "cancelled",
            "failed",
            "unknown",
        },
        "runtime_notice": {"completed"},
        "compaction_summary": {"completed"},
        "attachment": {"completed"},
        "extension": {
            "completed",
            "partial",
            "incomplete",
            "cancelled",
            "failed",
            "unknown",
        },
    }
    assert {key: set(value) for key, value in ALLOWED_PAYLOADS.items()} == expected_payloads
    assert {key: set(value) for key, value in ALLOWED_STATUSES.items()} == expected_statuses

    for semantic_kind, payloads in expected_payloads.items():
        for payload_kind in payloads:
            for status in expected_statuses[semantic_kind]:
                validate_item_compatibility(semantic_kind, payload_kind, status)


@pytest.mark.parametrize(
    ("semantic_kind", "payload_kind", "status"),
    [
        ([], "text", "completed"),
        ("user_input", {}, "completed"),
        ("user_input", "text", []),
        ("user_input", "tool_call", "completed"),
        ("runtime_notice", "text", "failed"),
    ],
)
def test_compatibility_validator_rejects_unhashable_and_matrix_values(
    semantic_kind: object, payload_kind: object, status: object
) -> None:
    with pytest.raises(ItemSchemaError):
        validate_item_compatibility(semantic_kind, payload_kind, status)


@pytest.mark.parametrize(
    ("selection_kind", "ref_type", "base_delta_role"),
    [
        ([], "canonical_item", "none"),
        ("canonical_history", [], "none"),
        ("canonical_history", "canonical_item", []),
        ("canonical_history", "request_only", "none"),
    ],
)
def test_selection_union_validator_fails_before_lookup(
    selection_kind: object, ref_type: object, base_delta_role: object
) -> None:
    with pytest.raises(ItemSchemaError, match="plan-order-integrity"):
        validate_selection_compatibility(selection_kind, ref_type, base_delta_role)


def test_selection_union_matrix_is_the_only_ref_dispatch() -> None:
    assert SELECTION_COMPATIBILITY == {
        "canonical_history": ("canonical_item", "none"),
        "request_only": ("request_only", "none"),
        "overlay_base": ("request_only", "base"),
        "overlay_delta": ("request_only", "delta"),
        "tool_set": ("tool_set", "none"),
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-0.0, b"0"),
        (1e23, b"1e+23"),
        (1e-6, b"0.000001"),
        (
            {"€": 1, "\r": 2, "דּ": 3, "1": 4, "😀": 5, "\u0080": 6, "ö": 7},
            '{"\\r":2,"1":4,"\u0080":6,"ö":7,"€":1,"😀":5,"דּ":3}'.encode(),
        ),
    ],
)
def test_jcs_golden_vectors_cover_numbers_unicode_and_utf16_keys(
    value: object, expected: bytes
) -> None:
    assert canonical_json_bytes(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        math.inf,
        -math.inf,
        math.nan,
        "\ud800",
        {1: "non-string key"},
    ],
)
def test_jcs_rejects_non_finite_invalid_unicode_and_invalid_json_shape(
    value: object,
) -> None:
    with pytest.raises(ItemSchemaError):
        canonical_json_bytes(value)


@pytest.mark.parametrize(
    ("value", "redacted"),
    [
        ("sha256:jcs:v1:" + "a" * 64, False),
        ("hmac-sha256:session:v1:" + "b" * 64, True),
        ("sha256:jcs:v1:" + "A" * 64, False),
        ("sha256:jcs:v1:short", False),
        ("redacted:known", True),
    ],
)
def test_manifest_hash_token_syntax_is_closed(value: str, redacted: bool) -> None:
    prefix, suffix = value.rsplit(":", 1)
    valid_hex = (
        len(suffix) == 64
        and suffix == suffix.lower()
        and all(character in "0123456789abcdef" for character in suffix)
    )
    expected_prefix = "hmac-sha256:session:v1" if redacted else "sha256:jcs:v1"
    if prefix == expected_prefix and valid_hex:
        validate_hash_token(value, "token", redacted=redacted)
    else:
        with pytest.raises(ItemSchemaError):
            validate_hash_token(value, "token", redacted=redacted)


@pytest.mark.parametrize("producer_kind", sorted(PRODUCER_KINDS))
def test_producer_kind_and_ref_shape_are_closed(producer_kind: str) -> None:
    assert validate_producer_ref(
        {"producer_kind": producer_kind, "producer_id": "producer-1"}
    )["producer_kind"] == producer_kind
    with pytest.raises(ItemSchemaError):
        validate_producer_ref(
            {"producer_kind": "model", "producer_id": "producer-1"}
        )
    with pytest.raises(ItemSchemaError):
        validate_producer_ref(
            {
                "producer_kind": producer_kind,
                "producer_id": "producer-1",
                "unknown": "not-a-schema-field",
            }
        )


@pytest.mark.parametrize("relation", sorted(PROVENANCE_RELATIONS))
def test_provenance_edge_keeps_explicit_relation_order_and_visibility(
    relation: str,
) -> None:
    edge = ProvenanceEdge(
        edge_id="edge-1",
        edge_idempotency_key="edge-key-1",
        relation=relation,
        source_ref="source-1",
        target_ref="target-1",
        produced_order=0,
        visibility="internal",
        protection="public",
    )
    assert edge.edge_idempotency_key == "edge-key-1"
    assert ProvenanceEdge(**asdict(edge)) == edge
    with pytest.raises(ItemSchemaError):
        ProvenanceEdge(
            edge_id="edge-1",
            edge_idempotency_key="edge-key-1",
            relation="unsupported",
            source_ref="source-1",
            target_ref="target-1",
        )


def test_context_ref_and_tool_set_ref_are_disjoint_selection_union(
    user_item,
) -> None:
    context_ref = ContextRef.canonical_item(user_item, session_id="session-1")
    tool_ref = ToolSetRef.from_tool_snapshot(
        snapshot_id="tools-1",
        session_id="session-1",
        plan_id="plan-1",
        tools=[],
        source_revision="tools-rev-1",
    )
    with pytest.raises(ItemSchemaError, match="plan-order-integrity"):
        ContextSelectionEntry(
            assembly_id="assembly-1",
            plan_ordinal=0,
            ref=context_ref,
            selection_kind="tool_set",
            included=False,
            omission_reason="not-a-tool-ref",
            loss=("union-mismatch",),
            availability="unavailable",
        )
    with pytest.raises(ItemSchemaError, match="plan-order-integrity"):
        ContextSelectionEntry(
            assembly_id="assembly-1",
            plan_ordinal=0,
            ref=tool_ref,
            selection_kind="canonical_history",
            included=False,
            omission_reason="not-a-canonical-ref",
            loss=("union-mismatch",),
            availability="unavailable",
        )
    with pytest.raises(ItemSchemaError):
        ContextRef.request_only_ref(
            "request-1",
            session_id="session-1",
            plan_id="plan-1",
            source_revision="revision-1",
            content="body",
            redacted_stable_digest="hmac-sha256:session:v1:" + "b" * 64,
            protection="public",
        )


def test_provenance_detail_reference_is_typed() -> None:
    detail_ref = DetailRef("session-1", "assembly-1", "detail-1")
    edge = ProvenanceEdge(
        edge_id="edge-detail",
        relation="derived_from",
        source_ref="source-1",
        target_ref="target-1",
        detail_ref=detail_ref,
    )
    assert edge.detail_ref == detail_ref
    with pytest.raises(ItemSchemaError):
        ProvenanceEdge(
            edge_id="edge-detail",
            relation="derived_from",
            source_ref="source-1",
            target_ref="target-1",
            detail_ref={"session_id": "session-1"},
        )
