"""显式 owner 不可由 parent envelope、同名 ref 或 omission 推断。"""

import json
from dataclasses import replace

import pytest

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_hash import context_request_hash
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry


@pytest.fixture(
    params=[
        "canonical_history",
        "request_only",
        "overlay_base",
        "overlay_delta",
        "tool_set",
    ]
)
def owned_omitted_snapshot(
    request: pytest.FixtureRequest,
    omitted_snapshot: ContextAssemblySnapshot,
) -> ContextAssemblySnapshot:
    session_id, plan_id = omitted_snapshot.session_id, omitted_snapshot.plan_id
    kind = request.param
    role = {"overlay_base": "base", "overlay_delta": "delta"}.get(kind, "none")
    if kind == "tool_set":
        ref = ToolSetRef.from_tool_snapshot(
            session_id=session_id,
            plan_id=plan_id,
            snapshot_id="owner-tools",
            source_revision="tools-r1",
            tools=[{"name": "owner_tool"}],
            assembly_id=omitted_snapshot.assembly_id,
        )
    else:
        ref = ContextRef(
            session_id=session_id, thread_id="thread-1",
            plan_id=None if kind == "canonical_history" else plan_id,
            ref_type="canonical_item"
            if kind == "canonical_history"
            else "request_only",
            ref_id="owner-ref",
            availability="unavailable",
            base_delta_role=role,
        )
    entry = ContextSelectionEntry(
        assembly_id=omitted_snapshot.assembly_id,
        plan_ordinal=0,
        ref=ref,
        selection_kind=kind,
        included=False,
        omission_reason="optional-unavailable",
        loss=("optional-unavailable",),
        availability=ref.availability,
        base_delta_role=role,
    )
    refs = (ref,) if isinstance(ref, ContextRef) else ()
    tools = (ref,) if isinstance(ref, ToolSetRef) else ()
    plan = ContextRequestPlan(
        session_id=session_id,
        plan_id=plan_id,
        refs=refs,
        tool_set_refs=tools,
        assembly_id=omitted_snapshot.assembly_id,
        plan_state="sealed",
        selection=(entry,),
    )
    return replace(
        omitted_snapshot,
        refs=refs,
        tool_set_refs=tools,
        selection=(entry,),
        plan_hash=plan.plan_hash(),
        request_hash=context_request_hash(plan, "provider-1", target_format="native"),
    )


def test_canonical_fact_can_be_selected_by_two_plans_in_its_session(
    user_item: CanonicalItemRecord,
) -> None:
    ref = ContextRef.canonical_item(user_item, session_id="session-owner", thread_id="thread-1")
    assert ref.plan_id is None
    fingerprints = []
    for plan_id in ("plan-owner-a", "plan-owner-b"):
        plan = ContextRequestPlan(
            session_id="session-owner", plan_id=plan_id, refs=(ref,)
        )
        assert plan.refs[0].session_id == "session-owner"
        assert plan.to_dict()["session_id"] == "session-owner"
        fingerprints.append(plan.plan_hash())
    assert fingerprints[0] == fingerprints[1]


def test_request_owner_is_plan_local_even_when_ref_ids_match() -> None:
    first = ContextRef.request_only_ref(
        "same-ref",
        session_id="session-owner", thread_id="thread-1",
        plan_id="plan-owner-a",
        source_revision="r1",
        content="body",
    )
    second = ContextRef.request_only_ref(
        "same-ref",
        session_id="session-owner", thread_id="thread-1",
        plan_id="plan-owner-b",
        source_revision="r1",
        content="body",
    )
    assert first != second
    for ref in (first, second):
        assert ContextRequestPlan(
            session_id=ref.session_id, plan_id=ref.plan_id, refs=(ref,)
        ).refs == (ref,)
    with pytest.raises(ItemSchemaError, match="plan"):
        ContextRequestPlan(
            session_id="session-owner", plan_id="plan-owner-b", refs=(first,)
        )


@pytest.mark.parametrize("api", ["ref", "canonical", "request", "tools", "plan"])
def test_owner_is_a_required_argument_not_a_default(
    user_item: CanonicalItemRecord,
    api: str,
) -> None:
    with pytest.raises(TypeError, match="session_id"):
        if api == "ref":
            ContextRef(
                ref_type="canonical_item", ref_id="ref", availability="unavailable"
            )
        elif api == "canonical":
            ContextRef.canonical_item(user_item)
        elif api == "request":
            ContextRef.request_only_ref(
                "ref", plan_id="plan-owner", source_revision="r1", content="body"
            )
        elif api == "tools":
            ToolSetRef.from_tool_snapshot(
                snapshot_id="tools",
                plan_id="plan-owner",
                source_revision="r1",
                tools=[],
            )
        else:
            ContextRequestPlan(plan_id="plan-owner", refs=())


def test_request_factory_requires_plan_argument() -> None:
    with pytest.raises(TypeError, match="plan_id"):
        ContextRef.request_only_ref(
            "ref", session_id="session-owner", thread_id="thread-1", source_revision="r1", content="body"
        )


@pytest.mark.parametrize("value", [None, "", 7, True])
@pytest.mark.parametrize("kind", ["canonical_item", "request_only", "tool_set", "plan"])
def test_invalid_session_owner_is_rejected(value: object, kind: str) -> None:
    with pytest.raises(ItemSchemaError, match="session_id"):
        if kind == "plan":
            ContextRequestPlan(session_id=value, plan_id="plan-owner", refs=())
        elif kind == "tool_set":
            ToolSetRef.from_tool_snapshot(
                session_id=value,
                snapshot_id="tools",
                plan_id="plan-owner",
                source_revision="r1",
                tools=[],
            )
        else:
            ContextRef(
                session_id=value, thread_id="thread-1",
                plan_id="plan-owner" if kind == "request_only" else None,
                ref_type=kind,
                ref_id="ref",
                availability="unavailable",
            )


@pytest.mark.parametrize("value", [None, "", 7, True])
def test_request_ref_cannot_omit_or_coerce_plan_owner(value: object) -> None:
    with pytest.raises(ItemSchemaError, match="plan_id"):
        ContextRef(
            session_id="session-owner", thread_id="thread-1",
            plan_id=value,
            ref_type="request_only",
            ref_id="ref",
            availability="unavailable",
        )


def test_canonical_ref_cannot_gain_plan_ownership(
    user_item: CanonicalItemRecord,
) -> None:
    ref = ContextRef.canonical_item(user_item, session_id="session-owner", thread_id="thread-1")
    with pytest.raises(ItemSchemaError, match="plan_id"):
        replace(ref, plan_id="not-canonical-owner")


@pytest.mark.parametrize("field", ["session_id", "plan_id"])
def test_omission_cannot_bypass_plan_or_snapshot_owner_checks(
    owned_omitted_snapshot: ContextAssemblySnapshot,
    field: str,
) -> None:
    snapshot = owned_omitted_snapshot
    entry = snapshot.selection[0]
    if field == "plan_id" and entry.ref.ref_type == "canonical_item":
        with pytest.raises(ItemSchemaError, match="plan_id"):
            replace(entry.ref, plan_id="wrong-plan")
        return
    wrong = replace(entry.ref, **{field: "wrong-owner"})
    wrong_entry = replace(entry, ref=wrong)
    refs = (wrong,) if isinstance(wrong, ContextRef) else ()
    tools = (wrong,) if isinstance(wrong, ToolSetRef) else ()
    # registry 与 entry 一同替换，不能靠 manifest 不一致偶然拒绝，而必须检查 parent owner。
    expected_plan_error = (
        "ToolSetRef 必须属于当前 ContextRequestPlan"
        if isinstance(wrong, ToolSetRef) and field == "plan_id"
        else f"source-mismatch: {type(wrong).__name__} 不属于当前 {field.removesuffix('_id')}"
    )
    with pytest.raises(ItemSchemaError, match=f"^{expected_plan_error}$"):
        ContextRequestPlan(
            session_id=snapshot.session_id,
            plan_id=snapshot.plan_id,
            refs=refs,
            tool_set_refs=tools,
            selection=(wrong_entry,),
            assembly_id=snapshot.assembly_id,
            plan_state="sealed",
        )
    expected_snapshot_error = (
        "ToolSetRef 与 assembly/plan 不一致"
        if isinstance(wrong, ToolSetRef)
        else "source-mismatch: ContextRef 与 assembly owner 不一致"
    )
    with pytest.raises(ItemSchemaError, match=f"^{expected_snapshot_error}$"):
        replace(snapshot, refs=refs, tool_set_refs=tools, selection=(wrong_entry,))


def test_owner_roundtrip_keeps_omitted_identity_without_allocating_body(
    owned_omitted_snapshot: ContextAssemblySnapshot,
) -> None:
    snapshot = owned_omitted_snapshot
    raw = json.loads(canonical_json_bytes(snapshot.to_dict()))
    restored = ContextAssemblySnapshot.from_dict(raw)
    restored.validate_hashes()
    ref = restored.selection[0].ref
    assert ref.session_id == snapshot.session_id
    assert ref.plan_id == (
        None if ref.ref_type == "canonical_item" else snapshot.plan_id
    )
    assert restored.to_dict() == raw
    assert restored.selection[0].detail_ref is None
    assert restored.selection[0].contribution_id is None
    assert restored.selection[0].contribution_ordinal is None


@pytest.mark.parametrize("field", ["session_id", "plan_id"])
def test_restore_never_infers_missing_ref_owner_from_enclosing_snapshot(
    owned_omitted_snapshot: ContextAssemblySnapshot,
    field: str,
) -> None:
    raw = owned_omitted_snapshot.to_dict()
    raw["selection"][0]["ref"].pop(field)
    with pytest.raises(ItemSchemaError, match="ContextRef|ToolSetRef"):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize("field", ["session_id", "plan_id"])
def test_restore_rejects_consistently_forged_omitted_owner(
    owned_omitted_snapshot: ContextAssemblySnapshot,
    field: str,
) -> None:
    raw = owned_omitted_snapshot.to_dict()
    raw["selection"][0]["ref"][field] = "forged-owner"
    for ref in raw["refs"]:
        ref[field] = "forged-owner"
    kind = raw["selection"][0]["ref"]["ref_type"]
    if kind == "canonical_item" and field == "plan_id":
        expected_error = "canonical ContextRef 不得携带 plan_id"
    elif kind == "tool_set":
        expected_error = "selection ToolSetRef scope 不一致"
    else:
        expected_error = "source-mismatch: ContextRef 与 assembly owner 不一致"
    with pytest.raises(ItemSchemaError, match=f"^{expected_error}$"):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize("kind", ["canonical_item", "request_only", "tool_set"])
def test_unselected_registry_cannot_hide_a_foreign_session(
    user_item: CanonicalItemRecord,
    kind: str,
) -> None:
    if kind == "canonical_item":
        ref = ContextRef.canonical_item(user_item, session_id="foreign-session", thread_id="thread-1")
    elif kind == "request_only":
        ref = ContextRef.request_only_ref(
            "foreign-ref",
            session_id="foreign-session", thread_id="thread-1",
            plan_id="plan-owner",
            source_revision="r1",
            content="body",
        )
    else:
        ref = ToolSetRef.from_tool_snapshot(
            session_id="foreign-session",
            plan_id="plan-owner",
            snapshot_id="foreign-tools",
            source_revision="r1",
            tools=[],
        )
    with pytest.raises(ItemSchemaError, match="session"):
        ContextRequestPlan(
            session_id="session-owner",
            plan_id="plan-owner",
            refs=(ref,) if isinstance(ref, ContextRef) else (),
            tool_set_refs=(ref,) if isinstance(ref, ToolSetRef) else (),
        )


@pytest.fixture(params=["canonical_history", "request_only", "tool_set"])
def owned_included_snapshot(
    request: pytest.FixtureRequest,
    omitted_snapshot: ContextAssemblySnapshot,
    user_item: CanonicalItemRecord,
) -> ContextAssemblySnapshot:
    snapshot = omitted_snapshot
    if request.param == "canonical_history":
        ref = ContextRef.canonical_item(user_item, session_id=snapshot.session_id, thread_id="thread-1")
    elif request.param == "request_only":
        ref = ContextRef.request_only_ref(
            "included-owner-ref",
            session_id=snapshot.session_id, thread_id="thread-1",
            plan_id=snapshot.plan_id,
            source_revision="r1",
            content="owner body",
            source_ref="source-owner",
        )
    else:
        ref = ToolSetRef.from_tool_snapshot(
            session_id=snapshot.session_id,
            plan_id=snapshot.plan_id,
            snapshot_id="included-owner-tools",
            tools=[{"name": "owner_tool"}],
            source_revision="tools-r1",
            assembly_id=snapshot.assembly_id,
        )
    entry = ContextSelectionEntry(
        assembly_id=snapshot.assembly_id,
        plan_ordinal=0,
        ref=ref,
        selection_kind=request.param,
        source_revision=ref.source_revision,
        content_length=ref.content_length,
        content_hash=ref.content_hash,
        detail_ref=DetailRef(snapshot.session_id, snapshot.assembly_id, "detail-owner")
        if isinstance(ref, ContextRef) and ref.ref_type == "request_only"
        else None,
    )
    plan = ContextRequestPlan(
        session_id=snapshot.session_id,
        plan_id=snapshot.plan_id,
        refs=(ref,) if isinstance(ref, ContextRef) else (),
        tool_set_refs=(ref,) if isinstance(ref, ToolSetRef) else (),
        assembly_id=snapshot.assembly_id,
        plan_state="sealed",
        selection=(entry,),
    )
    return replace(
        snapshot,
        refs=plan.refs,
        tool_set_refs=plan.tool_set_refs,
        selection=plan.selection,
        plan_hash=plan.plan_hash(),
        request_hash=context_request_hash(plan, "provider-1", target_format="native"),
        tool_snapshot=ref.tools if isinstance(ref, ToolSetRef) else (),
    )


def test_included_owner_roundtrip_is_exact(
    owned_included_snapshot: ContextAssemblySnapshot,
) -> None:
    raw = json.loads(canonical_json_bytes(owned_included_snapshot.to_dict()))
    restored = ContextAssemblySnapshot.from_dict(raw)
    restored.validate_hashes()
    assert restored == owned_included_snapshot
    assert restored.selection[0].ref.session_id == restored.session_id
    assert restored.selection[0].ref.plan_id == (
        None
        if restored.selection[0].ref.ref_type == "canonical_item"
        else restored.plan_id
    )


@pytest.mark.parametrize("field", ["session_id", "plan_id"])
def test_included_snapshot_rejects_foreign_parent_owner(
    owned_included_snapshot: ContextAssemblySnapshot,
    field: str,
) -> None:
    snapshot = owned_included_snapshot
    ref = snapshot.selection[0].ref
    if field == "plan_id" and ref.ref_type == "canonical_item":
        # canonical fact 不归某个 plan 所有，同 session 内重选无需重建 ref。
        assert replace(snapshot, plan_id="another-plan").refs == snapshot.refs
        return
    expected = (
        "ToolSetRef 与 assembly/plan 不一致"
        if isinstance(ref, ToolSetRef)
        else "source-mismatch: ContextRef 与 assembly owner 不一致"
    )
    with pytest.raises(ItemSchemaError, match=f"^{expected}$"):
        replace(snapshot, **{field: "foreign-owner"})


@pytest.mark.parametrize("field", ["session_id", "plan_id"])
def test_included_registry_restore_requires_owner_fields(
    owned_included_snapshot: ContextAssemblySnapshot,
    field: str,
) -> None:
    raw = owned_included_snapshot.to_dict()
    table = (
        "tool_set_refs"
        if raw["selection"][0]["ref"]["ref_type"] == "tool_set"
        else "refs"
    )
    raw[table][0].pop(field)
    with pytest.raises(
        ItemSchemaError, match="ToolSetRef 缺少字段|ContextRef 缺少 manifest 字段"
    ):
        ContextAssemblySnapshot.from_dict(raw)


def test_tool_ref_constructor_also_requires_session(
    golden_plan: ContextRequestPlan,
) -> None:
    raw = golden_plan.tool_set_refs[0].to_dict()
    raw.pop("session_id")
    with pytest.raises(TypeError, match="session_id"):
        ToolSetRef(**raw)


@pytest.mark.parametrize("field", ["session_id", "plan_id"])
def test_selected_owner_identity_is_bound_in_plan_and_request_hash(
    golden_plan: ContextRequestPlan,
    field: str,
) -> None:
    # 显式构造另一个合法 owner 的整套 source；不能只篡改 parent 后绕过验证。
    target_session = (
        "session-target" if field == "session_id" else golden_plan.session_id
    )
    target_plan = "plan-target" if field == "plan_id" else golden_plan.plan_id
    refs = tuple(replace(ref, session_id=target_session) for ref in golden_plan.refs)
    tools = tuple(
        replace(ref, session_id=target_session, plan_id=target_plan)
        for ref in golden_plan.tool_set_refs
    )
    by_identity = {(ref.ref_type, ref.ref_id): ref for ref in (*refs, *tools)}
    selection = tuple(
        replace(entry, ref=by_identity[(entry.ref.ref_type, entry.ref.ref_id)])
        for entry in golden_plan.selection
    )
    target = replace(
        golden_plan,
        session_id=target_session,
        plan_id=target_plan,
        refs=refs,
        tool_set_refs=tools,
        selection=selection,
    )
    assert target.plan_hash() != golden_plan.plan_hash()
    assert context_request_hash(target, "provider-1") != context_request_hash(
        golden_plan, "provider-1"
    )
    assert target.refs[0].plan_id is None
    assert target.refs[0].content_hash == golden_plan.refs[0].content_hash
    assert (
        target.tool_set_refs[0].content_hash
        == golden_plan.tool_set_refs[0].content_hash
    )
