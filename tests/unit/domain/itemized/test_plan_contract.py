from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.itemized.assembly_snapshot import (
    ContextAssemblySnapshot,
    context_request_hash,
)
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import (
    PayloadKind,
    SelectionKind,
)
from app.domain.itemized.errors import FormatDispatchError, ItemSchemaError
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_plan import (
    ContextContribution,
    ContextRequestPlan,
    resolve_contribution_for_ref,
)
from app.domain.itemized.selection import ContextSelectionEntry


@pytest.mark.parametrize("scope", ["ref", "selection", "selection_ref"])
@pytest.mark.parametrize("alias", ["ref_kind", "request_only"])
def test_restore_rejects_parallel_ref_discriminators(
    omitted_snapshot: ContextAssemblySnapshot,
    scope: str,
    alias: str,
) -> None:
    raw = omitted_snapshot.to_dict()
    target = {
        "ref": raw["refs"][0],
        "selection": raw["selection"][0],
        "selection_ref": raw["selection"][0]["ref"],
    }[scope]
    target[alias] = True
    with pytest.raises((ItemSchemaError, FormatDispatchError)):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize("field", ["assembly_id", "plan_ordinal", "unknown_field"])
def test_restore_does_not_add_scope_to_context_ref(
    omitted_snapshot: ContextAssemblySnapshot, field: str
) -> None:
    raw = omitted_snapshot.to_dict()
    raw["refs"][0][field] = "wrong-scope"
    with pytest.raises(ItemSchemaError, match="ContextRef"):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize("version", [True, 2.0, "2", None, 1])
def test_plan_and_selection_version_are_exact_integers(
    omitted_snapshot: ContextAssemblySnapshot, version: object
) -> None:
    with pytest.raises(ItemSchemaError):
        ContextRequestPlan(
            session_id=omitted_snapshot.session_id,
            plan_id="plan",
            refs=(),
            format_version=version,
        )
    raw = omitted_snapshot.to_dict()
    raw["selection"][0]["format_version"] = version
    with pytest.raises(ItemSchemaError):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.fixture
def prompt_contribution() -> ContextContribution:
    return ContextContribution(
        contribution_id="contribution-strict",
        source_kind="workspace",
        source_revision="rev-1",
        body="prompt",
        content_hash=contribution_content_hash("prompt", "prompt"),
        source_ordinal=0,
    )


def test_contribution_alias_is_resolved_from_explicit_source_metadata(
    prompt_contribution: ContextContribution,
) -> None:
    contribution = replace(
        prompt_contribution,
        metadata={"source_ref": "workspace:policy"},
        source_ordinal=0,
    )
    ref = ContextRef.request_only_ref(
        "plan-item-alias",
        session_id="session-alias",
        plan_id="plan-alias",
        source_revision=contribution.source_revision,
        source_ref="workspace:policy",
        content_length=contribution.content_length,
        content_hash_value=contribution.content_hash,
    )
    assert resolve_contribution_for_ref(ref, (contribution,)) == contribution
    with pytest.raises(ValueError, match="多个 contribution"):
        resolve_contribution_for_ref(
            ref, (contribution, replace(contribution, contribution_id="duplicate"))
        )


@pytest.mark.parametrize("role", ["base", "delta"])
def test_overlay_binding_requires_ref_role_and_epoch(
    prompt_contribution: ContextContribution, role: str
) -> None:
    kind = f"overlay_{role}"
    contribution = replace(
        prompt_contribution,
        contribution_kind=kind,
        content_hash=contribution_content_hash(kind, prompt_contribution.body),
        metadata={
            "overlay_ref": "overlay-1",
            "overlay_role": role,
            "source_overlay_epoch": 2,
        },
        source_ordinal=0,
    )
    ref = ContextRef.request_only_ref(
        "overlay-1",
        session_id="session-overlay",
        plan_id="plan-overlay",
        source_revision=contribution.source_revision,
        source_ref="workspace:policy",
        content_length=contribution.content_length,
        content_hash_value=contribution.content_hash,
        base_delta_role=role,
        source_overlay_epoch=2,
        overlay_from_revision="revision-before" if role == "delta" else None,
        overlay_to_revision="revision-after" if role == "delta" else None,
        overlay_diff_hash="sha256:jcs:v1:" + "a" * 64 if role == "delta" else None,
    )
    assert resolve_contribution_for_ref(ref, (contribution,)) == contribution
    with pytest.raises(ValueError, match="缺少唯一 contribution"):
        resolve_contribution_for_ref(
            replace(ref, source_overlay_epoch=3), (contribution,)
        )


@pytest.mark.parametrize(
    "field",
    [
        "request_only",
        "source_revision",
        "content_length",
        "contribution_kind",
        "assembly_id",
        "contribution_ordinal",
    ],
)
def test_restore_requires_contribution_manifest_fields(
    omitted_snapshot: ContextAssemblySnapshot,
    prompt_contribution: ContextContribution,
    field: str,
) -> None:
    from dataclasses import asdict

    raw = omitted_snapshot.to_dict()
    manifest = asdict(
        replace(
            prompt_contribution,
            assembly_id=omitted_snapshot.assembly_id,
            contribution_ordinal=0,
        )
    )
    manifest.pop(field)
    raw["contributions"] = [manifest]
    with pytest.raises(ItemSchemaError, match="ContextContribution"):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize("ref_type", ["canonical_item", "request_only"])
def test_restore_preserves_an_unavailable_ref_without_inventing_source_identity(
    omitted_snapshot: ContextAssemblySnapshot,
    ref_type: str,
) -> None:
    ref = ContextRef(
        session_id=omitted_snapshot.session_id,
        plan_id=omitted_snapshot.plan_id if ref_type == "request_only" else None,
        ref_type=ref_type,
        ref_id="unavailable-source",
        availability="unavailable",
    )
    entry = ContextSelectionEntry(
        assembly_id=omitted_snapshot.assembly_id,
        plan_ordinal=0,
        ref=ref,
        selection_kind="canonical_history"
        if ref_type == "canonical_item"
        else "request_only",
        included=False,
        omission_reason="source-unavailable",
        loss=("source-unavailable",),
        availability="unavailable",
    )
    plan = ContextRequestPlan(
        session_id=omitted_snapshot.session_id,
        plan_id=omitted_snapshot.plan_id,
        refs=(ref,),
    ).seal_for_assembly(
        omitted_snapshot.assembly_id,
        selection=(entry,),
    )
    snapshot = replace(
        omitted_snapshot,
        refs=(ref,),
        selection=(entry,),
        plan_hash=plan.plan_hash(),
        request_hash=context_request_hash(plan, "provider-1", target_format="native"),
    )
    restored = ContextAssemblySnapshot.from_dict(snapshot.to_dict())
    assert restored.refs[0].source_ref is None
    assert restored.refs[0].source_revision is None
    assert restored.selection[0].content_hash is None
    restored.validate_hashes()


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_contribution_request_only_is_an_intrinsic_true(
    prompt_contribution: ContextContribution, value: object
) -> None:
    with pytest.raises(ItemSchemaError, match="request_only"):
        replace(prompt_contribution, request_only=value)


@pytest.mark.parametrize("kind", ["tool_set", "unknown", "assistant_text"])
def test_contribution_kind_is_closed(
    prompt_contribution: ContextContribution, kind: str
) -> None:
    with pytest.raises(ItemSchemaError, match="contribution-kind-unsupported"):
        replace(prompt_contribution, contribution_kind=kind)


def test_unsealed_plan_has_no_selection_or_assembly(
    user_item: CanonicalItemRecord,
) -> None:
    ref = ContextRef.canonical_item(user_item, session_id="session-1")
    plan = ContextRequestPlan(session_id="session-1", plan_id="plan-draft", refs=(ref,))
    assert plan.assembly_id is None and plan.selection == ()
    with pytest.raises(ItemSchemaError, match="unsealed"):
        replace(plan, assembly_id="premature-assembly")
    with pytest.raises(ItemSchemaError):
        replace(plan, refs=(ref, ref))


@pytest.mark.parametrize("ordinal", [1, -1, True])
def test_selection_order_cannot_be_invented(
    omitted_snapshot: ContextAssemblySnapshot, ordinal: int
) -> None:
    raw = omitted_snapshot.to_dict()
    raw["selection"][0]["plan_ordinal"] = ordinal
    with pytest.raises(ItemSchemaError):
        ContextAssemblySnapshot.from_dict(raw)


def test_sealed_plan_freezes_selection_and_contribution_ordinals(
    user_item: CanonicalItemRecord,
) -> None:
    item_ref = ContextRef.canonical_item(user_item, session_id="session-1")
    body = {"text": "必须先读取配置"}
    contribution = ContextContribution(
        contribution_id="contribution-1",
        source_kind="system_policy",
        source_revision="policy-rev-1",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
        content_length=len(canonical_json_bytes(body)),
        source_ordinal=0,
    )
    request_ref = ContextRef.request_only_ref(
        "contribution-1",
        session_id="session-1",
        plan_id="plan-1",
        source_revision=contribution.source_revision,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        content_length=contribution.content_length,
        content_hash_value=contribution.content_hash,
        source_ref="policy-1",
    )
    selection = ContextSelectionEntry(
        assembly_id="assembly-1",
        plan_ordinal=0,
        ref=item_ref,
        selection_kind=SelectionKind.CANONICAL_HISTORY,
        source_revision=item_ref.source_revision,
        content_length=item_ref.content_length,
        content_hash=item_ref.content_hash,
        visibility=item_ref.visibility,
        protection=item_ref.protection,
        availability=item_ref.availability,
    )
    request_selection = ContextSelectionEntry(
        assembly_id="assembly-1",
        plan_ordinal=1,
        ref=request_ref,
        selection_kind=SelectionKind.REQUEST_ONLY,
        source_revision=request_ref.source_revision,
        content_length=request_ref.content_length,
        content_hash=request_ref.content_hash,
        visibility=request_ref.visibility,
        protection=request_ref.protection,
        availability=request_ref.availability,
        detail_ref=DetailRef("session-1", "assembly-1", "detail-1"),
        contribution_id=contribution.contribution_id,
        contribution_ordinal=0,
    )
    plan = ContextRequestPlan(
        session_id="session-1",
        plan_id="plan-1",
        refs=(item_ref, request_ref),
        contributions=(contribution,),
    )
    sealed = plan.seal_for_assembly(
        "assembly-1",
        selection=(selection, request_selection),
    )
    assert sealed.plan_state == "sealed"
    assert sealed.assembly_id == "assembly-1"
    assert sealed.contributions[0].assembly_id == "assembly-1"
    assert sealed.contributions[0].contribution_ordinal == 0
    assert sealed.plan_hash().startswith("sha256:jcs:v1:")
    assert sealed.seal_for_assembly(
        "assembly-1", selection=(selection, request_selection)
    ) == sealed
    with pytest.raises(ItemSchemaError, match="assembly-idempotency-conflict"):
        sealed.seal_for_assembly("assembly-1", selection=(selection,))
    with pytest.raises(ItemSchemaError, match="只能从 unsealed seal 一次"):
        sealed.seal_for_assembly("assembly-2", selection=(selection,))


def test_contribution_security_manifest_survives_plan_serialization_and_hash_scope() -> (
    None
):
    digest = "hmac-sha256:session:v1:" + "a" * 64
    contribution = ContextContribution(
        contribution_id="protected-contribution",
        source_kind="workspace_secret",
        source_revision="secret-revision",
        redacted_stable_digest=digest,
        content_length=42,
        visibility="private",
        protection="redacted",
        source_ordinal=0,
    )
    plan = ContextRequestPlan(
        session_id="session-protected-contribution",
        plan_id="plan-protected-contribution",
        refs=(),
        contributions=(contribution,),
    )

    serialized = plan.to_dict()
    row = serialized["contributions"][0]
    assert row["visibility"] == "private"
    assert row["protection"] == "redacted"
    assert row["body"] is None
    assert plan.plan_hash().startswith("sha256:jcs:v1:")


@pytest.mark.parametrize(
    "field_name",
    ["history_view_revision", "source_overlay_epoch"],
)
def test_plan_rejects_boolean_revision_coordinates(field_name: str) -> None:
    with pytest.raises(ItemSchemaError, match="非负整数"):
        ContextRequestPlan(
            session_id="session-invalid-revision",
            plan_id="plan-invalid-revision",
            refs=(),
            **{field_name: True},
        )


@pytest.mark.parametrize("contribution_id", [None, "wrong-contribution"])
def test_seal_rejects_included_contribution_binding_drift(
    contribution_id: str | None,
) -> None:
    body = {"text": "必须保留 provenance"}
    contribution = ContextContribution(
        contribution_id="contribution-bound",
        source_kind="system_policy",
        source_revision="policy-rev-bound",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
        content_length=len(canonical_json_bytes(body)),
        source_ordinal=0,
    )
    request_ref = ContextRef.request_only_ref(
        contribution.contribution_id,
        session_id="session-bound",
        plan_id="plan-bound",
        source_revision=contribution.source_revision,
        payload_kind=PayloadKind.STRUCTURED_CONTENT,
        content_length=contribution.content_length,
        content_hash_value=contribution.content_hash,
        source_ref="policy-bound",
    )
    selection = ContextSelectionEntry(
        assembly_id="assembly-bound",
        plan_ordinal=0,
        ref=request_ref,
        selection_kind=SelectionKind.REQUEST_ONLY,
        source_revision=request_ref.source_revision,
        content_length=request_ref.content_length,
        content_hash=request_ref.content_hash,
        visibility=request_ref.visibility,
        protection=request_ref.protection,
        availability=request_ref.availability,
        detail_ref=DetailRef("session-bound", "assembly-bound", "detail-bound"),
        contribution_id=contribution_id,
        contribution_ordinal=0 if contribution_id is not None else None,
    )
    plan = ContextRequestPlan(
        session_id="session-bound",
        plan_id="plan-bound",
        refs=(request_ref,),
        contributions=(contribution,),
    )

    with pytest.raises(ItemSchemaError, match="contribution_id"):
        plan.seal_for_assembly("assembly-bound", selection=(selection,))


def test_assembly_restore_preserves_omitted_selection_without_ref_hash_fallback(
    omitted_snapshot: ContextAssemblySnapshot,
) -> None:
    snapshot = omitted_snapshot

    restored = ContextAssemblySnapshot.from_dict(snapshot.to_dict())

    assert restored.selection[0].included is False
    assert restored.selection[0].content_length is None
    assert restored.selection[0].content_hash is None
    assert restored.selection[0].redacted_stable_digest is None
    restored.validate_hashes()


@pytest.mark.parametrize(
    "field_name",
    [
        "format_version",
        "projector_id",
        "projector_version",
        "target_format",
        "request_hash_preimage",
        "active_view_id",
        "selection_policy",
        "model_call_id",
        "hash_algorithm",
        "loss",
        "plan_state",
    ],
)
def test_assembly_restore_requires_explicit_envelope_fields(
    omitted_snapshot: ContextAssemblySnapshot, field_name: str
) -> None:
    raw = omitted_snapshot.to_dict()
    raw.pop(field_name)
    with pytest.raises(ItemSchemaError):
        ContextAssemblySnapshot.from_dict(raw)


def test_assembly_restore_requires_explicit_nullable_context_ref_fields(
    omitted_snapshot: ContextAssemblySnapshot,
) -> None:
    raw = omitted_snapshot.to_dict()
    raw["refs"][0].pop("source_ref")
    with pytest.raises(ItemSchemaError, match="ContextRef 缺少 manifest 字段"):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize("field_name", ["refs", "tool_snapshot", "loss"])
def test_assembly_snapshot_rejects_mutable_container_fields(
    omitted_snapshot: ContextAssemblySnapshot, field_name: str
) -> None:
    snapshot = omitted_snapshot
    with pytest.raises(ItemSchemaError, match="tuple"):
        replace(snapshot, **{field_name: list(getattr(snapshot, field_name))})


def test_assembly_snapshot_does_not_persist_unselected_tool_definitions(
    omitted_snapshot: ContextAssemblySnapshot,
) -> None:
    snapshot = omitted_snapshot
    tool_ref = ToolSetRef.from_tool_snapshot(
        session_id=snapshot.session_id,
        snapshot_id="tool-set-unselected",
        plan_id=snapshot.plan_id,
        assembly_id=snapshot.assembly_id,
        source_revision="tools-revision-unselected",
        tools=[{"type": "function", "function": {"name": "secret_tool"}}],
    )
    with_unselected_tool = replace(snapshot, tool_set_refs=(tool_ref,))
    assert with_unselected_tool.to_dict()["tool_snapshot"] == []
    assert with_unselected_tool.to_dict()["tool_set_refs"] == []

    selected = ContextSelectionEntry(
        assembly_id=snapshot.assembly_id,
        plan_ordinal=0,
        ref=tool_ref,
        selection_kind=SelectionKind.TOOL_SET,
        source_revision=tool_ref.source_revision,
        content_length=tool_ref.content_length,
        content_hash=tool_ref.content_hash,
        protection=tool_ref.protection,
        availability=tool_ref.availability,
    )
    with_selected_tool = replace(
        with_unselected_tool,
        selection=(selected,),
    )
    assert with_selected_tool.to_dict()["tool_snapshot"] == [
        {"type": "function", "function": {"name": "secret_tool"}}
    ]
    assert with_selected_tool.to_dict()["tool_set_refs"] == [tool_ref.to_dict()]


def test_assembly_snapshot_rejects_unnormalized_request_hash_preimage(
    omitted_snapshot: ContextAssemblySnapshot,
) -> None:
    snapshot = omitted_snapshot
    with pytest.raises(ItemSchemaError, match="request_hash_preimage"):
        replace(
            snapshot,
            request_hash_preimage={
                "model": "provider-model",
                "headers": {"authorization": "secret"},
            },
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("history_view_revision", True),
        ("source_overlay_epoch", "0"),
        ("sealed", "true"),
    ],
)
def test_assembly_restore_rejects_coerced_scalar_types(
    omitted_snapshot: ContextAssemblySnapshot,
    field_name: str,
    value: object,
) -> None:
    raw = omitted_snapshot.to_dict()
    raw[field_name] = value
    with pytest.raises(ItemSchemaError):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("ref_type", "canonical_item"),
        ("tool_policy", []),
        ("tools", "not-an-array"),
    ],
)
def test_assembly_restore_rejects_malformed_tool_set_manifest(
    omitted_snapshot: ContextAssemblySnapshot,
    field_name: str,
    value: object,
) -> None:
    snapshot = omitted_snapshot
    tool_ref = ToolSetRef.from_tool_snapshot(
        session_id=snapshot.session_id,
        snapshot_id="tool-set-restore",
        plan_id=snapshot.plan_id,
        assembly_id=snapshot.assembly_id,
        source_revision="tools-rev-restore",
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    raw = snapshot.to_dict()
    malformed_tool = dict(tool_ref.to_dict())
    malformed_tool[field_name] = value
    raw["tool_set_refs"] = [malformed_tool]
    with pytest.raises(ItemSchemaError):
        ContextAssemblySnapshot.from_dict(raw)
