"""typed source 与 sealed final detail 的 owner、serde 和 hash 合同。"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes, content_hash, sha256_jcs
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_hash import context_request_hash
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry


@pytest.fixture(scope="session")
def typed_detail_vectors() -> dict[str, object]:
    return json.loads(
        (Path.cwd() / "tests/fixtures/itemized/typed_detail_vectors.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.fixture
def typed_detail_plan(typed_detail_vectors) -> ContextRequestPlan:
    scenario = typed_detail_vectors["scenario"]
    source = DetailRef(**scenario["source_ref"])
    final = DetailRef(**scenario["detail_ref"])
    ref = ContextRef.request_only_ref(
        scenario["ref_id"],
        session_id=scenario["session_id"],
        plan_id=scenario["plan_id"],
        source_revision=scenario["source_revision"],
        payload_kind="text",
        content=scenario["body"],
        source_ref=source,
    )
    selection = ContextSelectionEntry(
        assembly_id=scenario["assembly_id"],
        plan_ordinal=0,
        ref=ref,
        selection_kind="request_only",
        source_revision=ref.source_revision,
        content_length=ref.content_length,
        content_hash=ref.content_hash,
        detail_ref=final,
    )
    return ContextRequestPlan(
        session_id=scenario["session_id"],
        plan_id=scenario["plan_id"],
        refs=(ref,),
    ).seal_for_assembly(scenario["assembly_id"], selection=(selection,))


@pytest.fixture
def typed_detail_snapshot(typed_detail_plan, typed_detail_vectors):
    plan = typed_detail_plan
    provider = typed_detail_vectors["scenario"]["provider"]
    return ContextAssemblySnapshot(
        assembly_id=plan.assembly_id,
        session_id=plan.session_id,
        turn_id="turn-detail",
        execution_id="execution-detail",
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash(),
        request_hash=context_request_hash(plan, provider, target_format="native"),
        history_view_revision=0,
        source_overlay_epoch=0,
        refs=plan.refs,
        contributions=(),
        tool_snapshot=(),
        compiler_version=plan.compiler_version,
        provider_version=provider,
        selection=plan.selection,
        sealed=True,
        plan_state="sealed",
        target_format="native",
    )


def test_detail_ref_exact_immutable_roundtrip(typed_detail_vectors):
    expected = typed_detail_vectors["scenario"]["detail_ref"]
    ref = DetailRef(**expected)
    assert ref.to_dict() == expected
    assert DetailRef.from_dict(json.loads(canonical_json_bytes(ref.to_dict()))) == ref
    with pytest.raises(FrozenInstanceError):
        ref.detail_id = "changed"
    returned = ref.to_dict()
    returned["detail_id"] = "changed"
    assert ref.detail_id == expected["detail_id"]


@pytest.mark.parametrize("field", ["session_id", "assembly_id", "detail_id"])
@pytest.mark.parametrize("invalid", [None, "", True, 3, "embedded\x00nul"])
def test_detail_ref_rejects_invalid_owner_identity(
    typed_detail_vectors, field, invalid
):
    raw = {**typed_detail_vectors["scenario"]["detail_ref"], field: invalid}
    with pytest.raises(ItemSchemaError, match=field):
        DetailRef(**raw)


@pytest.mark.parametrize("field", ["assembly_id", "detail_id"])
@pytest.mark.parametrize("invalid", [".", "..", "a/b", "a\\b"])
def test_detail_ref_rejects_path_segments(typed_detail_vectors, field, invalid):
    raw = {**typed_detail_vectors["scenario"]["detail_ref"], field: invalid}
    with pytest.raises(ItemSchemaError, match=field):
        DetailRef.from_dict(raw)


@pytest.mark.parametrize("field", ["session_id", "assembly_id", "detail_id"])
def test_detail_ref_restore_does_not_fill_missing_owner(typed_detail_vectors, field):
    raw = dict(typed_detail_vectors["scenario"]["detail_ref"])
    raw.pop(field)
    with pytest.raises(ItemSchemaError, match="完整且无额外字段"):
        DetailRef.from_dict(raw)


@pytest.mark.parametrize("raw", ["detail-only", None, [], {}, {"detail_id": "detail"}])
def test_detail_ref_restore_rejects_untyped_input(raw):
    with pytest.raises(ItemSchemaError, match="typed owner reference"):
        DetailRef.from_dict(raw)


def test_detail_ref_restore_rejects_extra_path(typed_detail_vectors):
    raw = {**typed_detail_vectors["scenario"]["detail_ref"], "path": "details/body"}
    with pytest.raises(ItemSchemaError, match="完整且无额外字段"):
        DetailRef.from_dict(raw)


@pytest.mark.parametrize(
    "session,assembly",
    [
        ("other-session", None),
        ("other-session", "assembly-final"),
        ("session-detail", "other-assembly"),
    ],
)
def test_detail_ref_rejects_foreign_owner(typed_detail_vectors, session, assembly):
    ref = DetailRef(**typed_detail_vectors["scenario"]["detail_ref"])
    with pytest.raises(ItemSchemaError, match="source-mismatch"):
        ref.require_owner(session, assembly)


def test_draft_source_and_sealed_final_have_distinct_bindings(typed_detail_plan):
    plan = typed_detail_plan
    ref = plan.refs[0]
    source = ref.source_ref
    final = plan.selection[0].detail_ref
    assert isinstance(source, DetailRef) and isinstance(final, DetailRef)
    source.require_owner(plan.session_id)
    final.require_owner(plan.session_id, plan.assembly_id)
    assert source.assembly_id == "assembly-source"
    assert final.assembly_id == "assembly-final"
    assert source != final
    assert not hasattr(ref, "detail_ref")
    assert "detail_ref" not in ref.to_dict()
    draft = ContextRequestPlan(
        session_id=plan.session_id, plan_id=plan.plan_id, refs=(ref,)
    )
    assert draft.assembly_id is None and draft.selection == ()
    assert draft.refs[0].source_ref == source


def test_typed_source_may_use_another_assembly_but_not_another_session(
    typed_detail_plan,
):
    ref = typed_detail_plan.refs[0]
    changed = replace(
        ref, source_ref=DetailRef(ref.session_id, "previous-assembly", "source")
    )
    assert changed.source_ref.assembly_id == "previous-assembly"
    with pytest.raises(ItemSchemaError, match="source-mismatch"):
        replace(
            ref, source_ref=DetailRef("foreign-session", "previous-assembly", "source")
        )


@pytest.mark.parametrize(
    "final",
    [
        None,
        "detail-final",
        DetailRef("session-detail", "assembly-final", "detail-final"),
    ],
)
def test_context_ref_constructor_rejects_final_detail_field(typed_detail_plan, final):
    with pytest.raises(TypeError, match="detail_ref"):
        replace(typed_detail_plan.refs[0], detail_ref=final)
    with pytest.raises(TypeError, match="detail_ref"):
        ContextRef.request_only_ref(
            "request",
            session_id="session-detail",
            plan_id="plan-detail",
            source_revision="r1",
            source_ref="policy",
            content="body",
            detail_ref=final,
        )


@pytest.mark.parametrize(
    "invalid",
    [
        "detail-final",
        {
            "session_id": "session-detail",
            "assembly_id": "assembly-final",
            "detail_id": "detail-final",
        },
    ],
)
def test_final_selection_constructor_requires_typed_object(typed_detail_plan, invalid):
    with pytest.raises(ItemSchemaError, match="typed DetailRef"):
        replace(typed_detail_plan.selection[0], detail_ref=invalid)


@pytest.mark.parametrize("field", ["session_id", "assembly_id"])
def test_final_selection_rejects_cross_owner(typed_detail_plan, field):
    entry = typed_detail_plan.selection[0]
    foreign = replace(entry.detail_ref, **{field: "foreign-owner"})
    with pytest.raises(ItemSchemaError, match="source-mismatch"):
        replace(entry, detail_ref=foreign)


def test_included_final_binding_cannot_fall_back_to_source(typed_detail_plan):
    entry = typed_detail_plan.selection[0]
    with pytest.raises(ItemSchemaError, match="必须绑定 detail_ref"):
        replace(entry, detail_ref=None)
    with pytest.raises(ItemSchemaError, match="source-mismatch"):
        replace(entry, detail_ref=entry.ref.source_ref)


def test_omission_cannot_allocate_final_detail(typed_detail_plan):
    entry = typed_detail_plan.selection[0]
    with pytest.raises(ItemSchemaError, match="omitted selection 不得分配"):
        replace(entry, included=False, omission_reason="budget", loss=("budget",))


def test_snapshot_roundtrip_preserves_both_typed_owners(typed_detail_snapshot):
    snapshot = typed_detail_snapshot
    encoded = canonical_json_bytes(snapshot.to_dict())
    restored = ContextAssemblySnapshot.from_dict(json.loads(encoded))
    assert restored == snapshot
    assert canonical_json_bytes(restored.to_dict()) == encoded
    assert isinstance(restored.refs[0].source_ref, DetailRef)
    assert isinstance(restored.selection[0].detail_ref, DetailRef)
    assert restored.refs[0].source_ref != restored.selection[0].detail_ref


@pytest.mark.parametrize("location", ["source", "selected_source", "final"])
@pytest.mark.parametrize("field", ["session_id", "assembly_id", "detail_id"])
def test_snapshot_restore_rejects_incomplete_typed_detail(
    typed_detail_snapshot, location, field
):
    raw = typed_detail_snapshot.to_dict()
    target = (
        raw["refs"][0]["source_ref"]
        if location == "source"
        else raw["selection"][0]["ref"]["source_ref"]
        if location == "selected_source"
        else raw["selection"][0]["detail_ref"]
    )
    target.pop(field)
    with pytest.raises(ItemSchemaError, match="完整且无额外字段"):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize("location", ["source", "selected_source", "final"])
def test_snapshot_restore_rejects_foreign_detail_session(
    typed_detail_snapshot, location
):
    raw = typed_detail_snapshot.to_dict()
    target = (
        raw["refs"][0]["source_ref"]
        if location == "source"
        else raw["selection"][0]["ref"]["source_ref"]
        if location == "selected_source"
        else raw["selection"][0]["detail_ref"]
    )
    target["session_id"] = "foreign-session"
    with pytest.raises(ItemSchemaError, match="source-mismatch"):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize("location", ["registry", "selection"])
@pytest.mark.parametrize("final", [None, "detail-final"])
def test_snapshot_ref_rejects_even_nullable_final_detail_field(
    typed_detail_snapshot, location, final
):
    raw = typed_detail_snapshot.to_dict()
    target = raw["refs"][0] if location == "registry" else raw["selection"][0]["ref"]
    target["detail_ref"] = final
    with pytest.raises(ItemSchemaError, match="ContextRef 含未知或不属于 ref 的字段"):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize("invalid", ["detail-final", None])
def test_snapshot_selection_cannot_restore_bare_or_missing_final(
    typed_detail_snapshot, invalid
):
    raw = typed_detail_snapshot.to_dict()
    raw["selection"][0]["detail_ref"] = invalid
    with pytest.raises(ItemSchemaError):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize(
    "family",
    ["typed-detail-plan", "typed-detail-request-default", "typed-detail-request-wire"],
)
def test_typed_detail_matches_independent_preimage(
    typed_detail_plan, typed_detail_vectors, family
):
    row = next(row for row in typed_detail_vectors["preimages"] if row["id"] == family)
    scenario = typed_detail_vectors["scenario"]
    assert row["preimage"]["selection"][0]["detail_ref"] == scenario["detail_ref"]
    assert canonical_json_bytes(row["preimage"]) == row["canonical"].encode("utf-8")
    assert sha256_jcs(row["preimage"]) == row["hash"]
    actual = (
        typed_detail_plan.plan_hash()
        if family == "typed-detail-plan"
        else context_request_hash(
            typed_detail_plan,
            scenario["provider"],
            target_format="native",
            wire_request=scenario["wire_request"] if family.endswith("-wire") else None,
        )
    )
    assert actual == row["hash"]


def test_plan_hash_binds_final_detail_identity(typed_detail_plan):
    entry = typed_detail_plan.selection[0]
    changed = replace(
        entry, detail_ref=replace(entry.detail_ref, detail_id="another-final")
    )
    assert (
        replace(typed_detail_plan, selection=(changed,)).plan_hash()
        != typed_detail_plan.plan_hash()
    )


def test_request_only_manifest_uses_body_hash_not_canonical_item_hash(
    typed_detail_plan,
):
    ref = typed_detail_plan.refs[0]
    assert ref.payload_kind == "text" and ref.content_length == 4
    assert ref.content_hash == sha256_jcs("body")
    assert ref.content_hash != content_hash("text", "body")


@pytest.mark.parametrize("location", ["registry", "selection"])
def test_restore_rejects_source_identity_drift_within_same_owner(
    typed_detail_snapshot, location
):
    raw = typed_detail_snapshot.to_dict()
    ref = raw["refs"][0] if location == "registry" else raw["selection"][0]["ref"]
    ref["source_ref"]["detail_id"] = "different-source"
    with pytest.raises(ItemSchemaError, match="selection ContextRef manifest 不一致"):
        ContextAssemblySnapshot.from_dict(raw)
