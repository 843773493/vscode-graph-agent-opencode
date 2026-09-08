from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import (
    PayloadKind,
    SelectionKind,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    canonical_json_bytes,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.schema import validate_selection_compatibility
from app.domain.itemized.selection import ContextSelectionEntry


@pytest.mark.parametrize(
    "selection_kind",
    ["canonical_history", "request_only", "overlay_base", "overlay_delta", "tool_set"],
)
@pytest.mark.parametrize("ref_type", ["canonical_item", "request_only", "tool_set"])
@pytest.mark.parametrize("base_delta_role", ["none", "base", "delta"])
def test_complete_selection_tag_matrix(
    selection_kind: str, ref_type: str, base_delta_role: str
) -> None:
    expected = {
        "canonical_history": ("canonical_item", "none"),
        "request_only": ("request_only", "none"),
        "overlay_base": ("request_only", "base"),
        "overlay_delta": ("request_only", "delta"),
        "tool_set": ("tool_set", "none"),
    }
    if (ref_type, base_delta_role) == expected[selection_kind]:
        validate_selection_compatibility(selection_kind, ref_type, base_delta_role)
    else:
        with pytest.raises(ItemSchemaError, match="plan-order-integrity"):
            validate_selection_compatibility(selection_kind, ref_type, base_delta_role)


@pytest.fixture(
    params=[
        "canonical_history",
        "request_only",
        "overlay_base",
        "overlay_delta",
        "tool_set",
    ]
)
def omitted_entry(
    request: pytest.FixtureRequest, user_item: CanonicalItemRecord
) -> ContextSelectionEntry:
    kind = request.param
    role = {"overlay_base": "base", "overlay_delta": "delta"}.get(kind, "none")
    if kind == "canonical_history":
        ref = ContextRef(
            session_id="session-1",
            ref_type="canonical_item",
            ref_id=user_item.item_id,
            availability="unavailable",
        )
    elif kind == "tool_set":
        ref = replace(
            ToolSetRef.from_tool_snapshot(
                session_id="session-1",
                snapshot_id="tools-omitted",
                plan_id="plan-omitted",
                tools=[],
                source_revision="tools-revision",
                assembly_id="assembly-omitted",
            ),
            availability="unavailable",
        )
    else:
        ref = ContextRef(
            session_id="session-1",
            plan_id="plan-omitted",
            ref_type="request_only",
            ref_id="request-omitted",
            availability="unavailable",
            base_delta_role=role,
        )
    return ContextSelectionEntry(
        assembly_id="assembly-omitted",
        plan_ordinal=0,
        ref=ref,
        selection_kind=kind,
        included=False,
        omission_reason="source-unavailable",
        loss=("source-unavailable",),
        availability="unavailable",
        base_delta_role=role,
    )


def test_omission_preserves_tag_with_no_body_or_binding(
    omitted_entry: ContextSelectionEntry,
) -> None:
    raw = omitted_entry.to_dict()
    assert raw["ref"]["ref_type"] == omitted_entry.ref.ref_type
    assert raw["included"] is False
    for field in (
        "source_revision",
        "content_length",
        "content_hash",
        "redacted_stable_digest",
        "detail_ref",
        "contribution_ordinal",
    ):
        assert raw[field] is None
    assert raw["loss"] == ["source-unavailable"]
    assert "ref_kind" not in raw and "request_only" not in raw


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("detail_ref", DetailRef("session-1", "assembly-omitted", "detail-wrong")),
        ("contribution_ordinal", 0),
        ("loss", ()),
        ("omission_reason", None),
    ],
)
def test_omission_rejects_fabricated_bindings_or_missing_loss(
    omitted_entry: ContextSelectionEntry,
    field: str,
    value: object,
) -> None:
    with pytest.raises(ItemSchemaError):
        replace(omitted_entry, **{field: value})


@pytest.mark.parametrize("field", ["source_revision", "content_length", "content_hash"])
def test_included_selection_requires_full_manifest(
    user_item: CanonicalItemRecord, field: str
) -> None:
    ref = ContextRef.canonical_item(user_item, session_id="session-1")
    raw = {
        "assembly_id": "assembly",
        "plan_ordinal": 0,
        "ref": ref,
        "selection_kind": "canonical_history",
        "source_revision": ref.source_revision,
        "content_length": ref.content_length,
        "content_hash": ref.content_hash,
    }
    with pytest.raises(ItemSchemaError):
        ContextSelectionEntry(**{**raw, field: None})


@pytest.mark.parametrize(
    ("field", "value"),
    [("protection", "encrypted"), ("availability", "maybe"), ("visibility", "hidden")],
)
def test_ref_rejects_unknown_security_tags(
    user_item: CanonicalItemRecord, field: str, value: str
) -> None:
    with pytest.raises(ItemSchemaError):
        replace(
            ContextRef.canonical_item(user_item, session_id="session-1"),
            **{field: value},
        )


@pytest.mark.parametrize(
    "alias", ["ref_kind", "request_only", "assembly_id", "plan_ordinal"]
)
def test_context_ref_cannot_gain_an_alias_or_assembly_scope(
    user_item: CanonicalItemRecord, alias: str
) -> None:
    with pytest.raises(TypeError):
        ContextRef(
            **{
                **ContextRef.canonical_item(
                    user_item, session_id="session-1"
                ).to_dict(),
                alias: True,
            }
        )


def test_context_ref_binds_canonical_item_manifest(
    user_item: CanonicalItemRecord,
) -> None:
    ref = ContextRef.canonical_item(user_item, session_id="session-1")
    assert ref.ref_type == "canonical_item"
    assert ref.content_hash == user_item.content_hash
    assert ref.content_length == len("读取 README".encode())
    assert ref.to_dict()["content_hash"] == ref.content_hash


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("source_revision", 1),
        ("source_revision", ""),
        ("source_revision", None),
        ("visibility", 1),
        ("protection", None),
    ],
)
def test_canonical_ref_does_not_coerce_metadata_identity(
    user_item: CanonicalItemRecord,
    field_name: str,
    value: object,
) -> None:
    item = user_item
    metadata = dict(item.metadata)
    metadata[field_name] = value
    with pytest.raises(ItemSchemaError, match="CanonicalItemRecord.metadata"):
        ContextRef.canonical_item(
            replace(item, metadata=metadata), session_id="session-1"
        )


def test_request_only_ref_rejects_empty_source_and_unknown_payload_kind() -> None:
    with pytest.raises(ItemSchemaError, match="source_ref"):
        ContextRef.request_only_ref(
            "request-invalid-source",
            session_id="session-invalid-source",
            plan_id="plan-invalid-source",
            source_revision="revision-1",
            content="正文",
            source_ref="",
        )
    with pytest.raises(ItemSchemaError, match="payload_kind"):
        ContextRef.request_only_ref(
            "request-invalid-kind",
            session_id="session-invalid-kind",
            plan_id="plan-invalid-kind",
            source_revision="revision-1",
            payload_kind="not-a-payload-kind",
            content="正文",
            source_ref="source-1",
        )


def test_tool_set_snapshot_rejects_non_mapping_policy_or_tools() -> None:
    kwargs = {
        "session_id": "session-invalid",
        "snapshot_id": "tool-set-invalid",
        "plan_id": "plan-invalid",
        "source_revision": "tools-revision-1",
        "tools": [],
    }
    with pytest.raises(TypeError, match="tool_policy"):
        ToolSetRef.from_tool_snapshot(**kwargs, tool_policy=[])
    with pytest.raises(TypeError, match="tools 元素"):
        ToolSetRef.from_tool_snapshot(
            **{**kwargs, "tools": ["not-a-tool"]},
        )


def test_request_only_ref_requires_consistent_body_manifest() -> None:
    body = {"text": "项目约束", "source": "workspace"}
    ref = ContextRef.request_only_ref(
        "request-1",
        session_id="session-1",
        plan_id="plan-1",
        source_revision="workspace-rev-1",
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        content=body,
        source_ref="workspace-policy",
    )
    assert ref.content_hash is not None
    assert ref.content_length == len(canonical_json_bytes(body))
    with pytest.raises(ItemSchemaError, match="content_length"):
        ContextRef.request_only_ref(
            "request-1",
            session_id="session-1",
            plan_id="plan-1",
            source_revision="workspace-rev-1",
            payload_kind=PayloadKind.STRUCTURED_CONTENT,
            content=body,
            content_length=1,
            source_ref="workspace-policy",
        )


def test_tool_set_ref_has_stable_tool_order_and_manifest_hash() -> None:
    ref = ToolSetRef.from_tool_snapshot(
        session_id="session-1",
        snapshot_id="tools-1",
        plan_id="plan-1",
        source_revision="tools-rev-1",
        tools=[
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "read_file"}},
        ],
    )
    assert [tool["function"]["name"] for tool in ref.tools] == [
        "read_file",
        "write_file",
    ]
    ref.validate_manifest()


@pytest.mark.parametrize(
    ("selection_kind", "ref_type", "base_delta_role"),
    [
        (SelectionKind.REQUEST_ONLY, "canonical_item", "none"),
        (SelectionKind.CANONICAL_HISTORY, "request_only", "none"),
        (SelectionKind.OVERLAY_BASE, "request_only", "none"),
        (SelectionKind.OVERLAY_DELTA, "request_only", "base"),
        (SelectionKind.TOOL_SET, "request_only", "none"),
    ],
)
def test_selection_matrix_rejects_union_mismatch_before_projection(
    user_item: CanonicalItemRecord,
    selection_kind: str,
    ref_type: str,
    base_delta_role: str,
) -> None:
    ref = ContextRef.canonical_item(user_item, session_id="session-1")
    if ref_type == "request_only":
        ref = ContextRef.request_only_ref(
            "request-matrix",
            session_id="session-1",
            plan_id="plan-matrix",
            source_revision="rev-matrix",
            content="request",
            source_ref="source-matrix",
            base_delta_role=base_delta_role,
            source_overlay_epoch=0 if base_delta_role != "none" else None,
            overlay_from_revision=(
                "rev-before" if base_delta_role == "delta" else None
            ),
            overlay_to_revision=("rev-after" if base_delta_role == "delta" else None),
            overlay_diff_hash=(
                "sha256:jcs:v1:diff" if base_delta_role == "delta" else None
            ),
        )
    with pytest.raises(ItemSchemaError, match="plan-order-integrity"):
        ContextSelectionEntry(
            assembly_id="assembly-matrix",
            plan_ordinal=0,
            ref=ref,
            selection_kind=selection_kind,
            source_revision=ref.source_revision,
            content_length=ref.content_length,
            content_hash=ref.content_hash,
            redacted_stable_digest=ref.redacted_stable_digest,
            visibility=ref.visibility,
            protection=ref.protection,
            availability=ref.availability,
        )


def test_omitted_selection_cannot_carry_two_manifest_tokens(
    user_item: CanonicalItemRecord,
) -> None:
    ref = ContextRef.canonical_item(user_item, session_id="session-1")
    with pytest.raises(ItemSchemaError, match="不能同时携带"):
        ContextSelectionEntry(
            assembly_id="assembly-omitted-two-tokens",
            plan_ordinal=0,
            ref=ref,
            selection_kind=SelectionKind.CANONICAL_HISTORY,
            included=False,
            omission_reason="budget",
            loss=("budget",),
            visibility=ref.visibility,
            protection=ref.protection,
            availability=ref.availability,
            content_hash=ref.content_hash,
            redacted_stable_digest="redacted:known",
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("included", 1),
        ("plan_ordinal", True),
        ("source_overlay_epoch", -1),
        ("source_overlay_epoch", True),
        ("contribution_ordinal", -1),
        ("contribution_ordinal", True),
        ("omission_reason", 1),
    ],
)
def test_selection_rejects_non_contract_scalar_types(
    user_item: CanonicalItemRecord,
    field_name: str,
    value: object,
) -> None:
    ref = ContextRef.canonical_item(user_item, session_id="session-1")
    entry = {
        "assembly_id": "assembly-scalar-types",
        "plan_ordinal": 0,
        "ref": ref,
        "selection_kind": SelectionKind.CANONICAL_HISTORY,
        "included": False,
        "omission_reason": "budget",
        "loss": ("budget",),
        "visibility": ref.visibility,
        "protection": ref.protection,
        "availability": ref.availability,
    }
    entry[field_name] = value
    with pytest.raises(ItemSchemaError):
        ContextSelectionEntry(**entry)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("source_revision", "wrong-revision"),
        ("content_length", 999),
        ("content_hash", "sha256:jcs:v1:wrong"),
    ],
)
def test_omitted_selection_rejects_known_manifest_drift(
    user_item: CanonicalItemRecord,
    field_name: str,
    value: object,
) -> None:
    ref = ContextRef.canonical_item(user_item, session_id="session-1")
    entry = {
        "assembly_id": "assembly-omitted-drift",
        "plan_ordinal": 0,
        "ref": ref,
        "selection_kind": SelectionKind.CANONICAL_HISTORY,
        "included": False,
        "omission_reason": "budget",
        "loss": ("budget",),
        "visibility": ref.visibility,
        "protection": ref.protection,
        "availability": ref.availability,
    }
    entry[field_name] = value
    with pytest.raises(ItemSchemaError, match="manifest"):
        ContextSelectionEntry(**entry)
