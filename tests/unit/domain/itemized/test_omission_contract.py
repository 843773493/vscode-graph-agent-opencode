"""omitted selection 的 seal/restore 不解析正文或重新分配 contribution。"""

from dataclasses import replace

import pytest

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import contribution_content_hash
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_hash import context_request_hash
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry


@pytest.fixture
def omitted_tool_snapshot(
    omitted_snapshot: ContextAssemblySnapshot,
) -> ContextAssemblySnapshot:
    tool = ToolSetRef.from_tool_snapshot(
        session_id=omitted_snapshot.session_id,
        snapshot_id="omitted-tools",
        plan_id=omitted_snapshot.plan_id,
        assembly_id=omitted_snapshot.assembly_id,
        source_revision="tools-r1",
        tools=[{"name": "must_not_be_read"}],
        tool_policy={"mode": "must_not_be_read"},
    )
    entry = ContextSelectionEntry(
        assembly_id=omitted_snapshot.assembly_id,
        plan_ordinal=0,
        ref=tool,
        selection_kind="tool_set",
        included=False,
        omission_reason="budget",
        loss=("budget",),
    )
    plan = ContextRequestPlan(
        session_id=omitted_snapshot.session_id,
        plan_id=omitted_snapshot.plan_id,
        refs=(),
        tool_set_refs=(tool,),
        selection=(entry,),
        assembly_id=omitted_snapshot.assembly_id,
        plan_state="sealed",
    )
    return replace(
        omitted_snapshot,
        refs=(),
        selection=plan.selection,
        tool_set_refs=plan.tool_set_refs,
        plan_hash=plan.plan_hash(),
        request_hash=context_request_hash(plan, "provider-1", target_format="native"),
    )


def test_omitted_tools_restore_without_registry_or_tool_definitions(
    omitted_tool_snapshot: ContextAssemblySnapshot,
) -> None:
    raw = omitted_tool_snapshot.to_dict()
    assert raw["tool_set_refs"] == []
    assert raw["tool_snapshot"] == []
    assert "tools" not in raw["selection"][0]["ref"]
    assert "tool_policy" not in raw["selection"][0]["ref"]
    restored = ContextAssemblySnapshot.from_dict(raw)
    restored.validate_hashes()
    assert restored.to_dict() == raw
    assert restored.selection[0].ref.ref_type == "tool_set"
    assert restored.selection[0].contribution_id is None


@pytest.mark.parametrize("field", ["tools", "tool_policy", "ref_kind"])
def test_omitted_tool_identity_rejects_body_and_alias_fields(
    omitted_tool_snapshot: ContextAssemblySnapshot,
    field: str,
) -> None:
    raw = omitted_tool_snapshot.to_dict()
    raw["selection"][0]["ref"][field] = []
    with pytest.raises(ValueError, match="omitted ToolSetRef identity"):
        ContextAssemblySnapshot.from_dict(raw)


@pytest.mark.parametrize("role", ["base", "delta"])
@pytest.mark.parametrize("existing_id", [None, "already-registered-contribution"])
def test_available_omitted_overlay_seals_without_resolving_contribution(
    omitted_snapshot: ContextAssemblySnapshot,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    existing_id: str | None,
) -> None:
    ref = ContextRef.request_only_ref(
        "overlay-omitted",
        session_id=omitted_snapshot.session_id,
        plan_id=omitted_snapshot.plan_id,
        source_revision="revision-2",
        content={"text": "omitted"},
        base_delta_role=role,
        source_overlay_epoch=2,
        overlay_from_revision="revision-1" if role == "delta" else None,
        overlay_to_revision="revision-2" if role == "delta" else None,
        overlay_diff_hash="sha256:jcs:v1:" + "a" * 64 if role == "delta" else None,
    )
    entry = ContextSelectionEntry(
        assembly_id=omitted_snapshot.assembly_id,
        plan_ordinal=0,
        ref=ref,
        selection_kind=f"overlay_{role}",
        included=False,
        omission_reason="budget",
        loss=("budget",),
        base_delta_role=role,
        source_overlay_epoch=2,
        overlay_from_revision=ref.overlay_from_revision,
        overlay_to_revision=ref.overlay_to_revision,
        overlay_diff_hash=ref.overlay_diff_hash,
        contribution_id=existing_id,
    )

    def forbid_resolution(*args: object, **kwargs: object) -> None:
        pytest.fail("omitted entry 不得调用 active contribution resolver")

    for module in ("request_plan", "assembly_snapshot"):
        monkeypatch.setattr(
            f"app.domain.itemized.{module}.resolve_contribution_for_ref",
            forbid_resolution,
        )
    plan = ContextRequestPlan(
        session_id=omitted_snapshot.session_id,
        plan_id=omitted_snapshot.plan_id,
        refs=(ref,),
    ).seal_for_assembly(omitted_snapshot.assembly_id, selection=(entry,))
    snapshot = replace(
        omitted_snapshot,
        refs=plan.refs,
        selection=plan.selection,
        plan_hash=plan.plan_hash(),
        request_hash=context_request_hash(plan, "provider-1", target_format="native"),
    )
    restored = ContextAssemblySnapshot.from_dict(snapshot.to_dict())
    restored.validate_hashes()
    assert restored.contributions == ()
    assert restored.selection[0].contribution_id == existing_id
    assert restored.selection[0].contribution_ordinal is None
    assert restored.selection[0].detail_ref is None
    assert restored.selection[0].base_delta_role == role


@pytest.fixture
def overlay_chain() -> tuple[ContextRequestPlan, tuple[ContextSelectionEntry, ...]]:
    contributions = []
    entries = []
    for ordinal, role in enumerate(("base", "delta")):
        kind = f"overlay_{role}"
        body = {"text": role}
        contribution = ContextContribution(
            contribution_id=f"contribution-{role}",
            source_kind="workspace_policy",
            source_revision="A" if role == "base" else "B",
            content_hash=contribution_content_hash(kind, body),
            body=body,
            contribution_kind=kind,
            metadata={
                "overlay_id": "overlay-policy",
                "overlay_ref": f"ref-{role}",
                "overlay_role": role,
                "source_overlay_epoch": 1,
            },
            source_ordinal=ordinal,
        )
        ref = ContextRef.request_only_ref(
            f"ref-{role}",
            session_id="session-chain",
            plan_id="plan-chain",
            source_revision=contribution.source_revision,
            content_hash_value=contribution.content_hash,
            content_length=contribution.content_length,
            source_ref=f"overlay-policy:{role}",
            base_delta_role=role,
            source_overlay_epoch=1,
            overlay_from_revision="A" if role == "delta" else None,
            overlay_to_revision="B" if role == "delta" else None,
            overlay_diff_hash="sha256:jcs:v1:" + "a" * 64 if role == "delta" else None,
        )
        entries.append(
            ContextSelectionEntry(
                assembly_id="assembly-chain",
                plan_ordinal=ordinal,
                ref=ref,
                selection_kind=kind,
                source_revision=ref.source_revision,
                content_length=ref.content_length,
                content_hash=ref.content_hash,
                detail_ref=DetailRef("session-chain", "assembly-chain", f"detail-{role}"),
                contribution_id=contribution.contribution_id,
                contribution_ordinal=ordinal,
                base_delta_role=role,
                source_overlay_epoch=1,
                overlay_from_revision=ref.overlay_from_revision,
                overlay_to_revision=ref.overlay_to_revision,
                overlay_diff_hash=ref.overlay_diff_hash,
            )
        )
        contributions.append(contribution)
    return ContextRequestPlan(
        session_id="session-chain",
        plan_id="plan-chain",
        refs=tuple(entry.ref for entry in entries),
        contributions=tuple(contributions),
    ), tuple(entries)


def test_omitted_base_cannot_authorize_included_delta(
    overlay_chain: tuple[ContextRequestPlan, tuple[ContextSelectionEntry, ...]],
) -> None:
    draft, entries = overlay_chain
    omitted_base = replace(
        entries[0],
        included=False,
        omission_reason="budget",
        loss=("budget",),
        contribution_id=None,
        contribution_ordinal=None,
        detail_ref=None,
    )
    with pytest.raises(ValueError, match="overlay delta 缺少 included base/chain"):
        draft.seal_for_assembly(
            "assembly-chain",
            selection=(omitted_base, replace(entries[1], contribution_ordinal=0)),
        )


@pytest.mark.parametrize("mutation", ["none", "overlay_id", "revision", "order"])
def test_included_chain_requires_same_overlay_revision_and_order(
    overlay_chain: tuple[ContextRequestPlan, tuple[ContextSelectionEntry, ...]],
    mutation: str,
) -> None:
    draft, entries = overlay_chain
    if mutation == "overlay_id":
        delta = draft.contributions[1]
        draft = replace(
            draft,
            contributions=(
                draft.contributions[0],
                replace(
                    delta,
                    metadata={**delta.metadata, "overlay_id": "unrelated-overlay"},
                ),
            ),
        )
    elif mutation == "revision":
        delta_ref = replace(entries[1].ref, overlay_from_revision="wrong-base")
        entries = (
            entries[0],
            replace(entries[1], ref=delta_ref, overlay_from_revision="wrong-base"),
        )
        draft = replace(draft, refs=(draft.refs[0], delta_ref))
    elif mutation == "order":
        entries = (
            replace(entries[1], plan_ordinal=0),
            replace(entries[0], plan_ordinal=1),
        )
    if mutation == "none":
        assert (
            draft.seal_for_assembly("assembly-chain", selection=entries).plan_state
            == "sealed"
        )
    else:
        with pytest.raises(ValueError, match="overlay delta 缺少 included base/chain"):
            draft.seal_for_assembly("assembly-chain", selection=entries)
