"""Saver seal/read/重启中 typed detail owner、用途和物理 shape 的集成合同。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    DetailUnavailableError,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    _project,
    projection_draft,
    projection_plan,
    projection_saver,
    projection_workspace,
    register_projection_draft,
)

__all__ = [
    "projection_draft",
    "projection_plan",
    "projection_saver",
    "projection_workspace",
]


@pytest.mark.parametrize("change_identity", [False, True])
def test_plain_request_recovery_requires_explicit_source_identity(
    projection_saver, projection_draft, change_identity
) -> None:
    saver, session_id, _ = projection_saver
    draft, bodies = projection_draft
    draft = replace(draft, tool_set_refs=())
    draft = register_projection_draft(saver, session_id, draft)
    first = saver.seal_context_plan(
        session_id,
        draft,
        seal_idempotency_key=f"seal:{draft.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(session_id, turn_id="turn-1"),
        provider_version="contract-provider",
        request_only_content=bodies,
    )
    # 新 plan 的 source locator 指向旧 selection，最终 detail 则必须重新分配。
    source = (
        DetailRef(session_id, "unrelated-assembly", "unrelated-detail")
        if change_identity
        else first.selection[2].detail_ref
    )
    next_draft = replace(
        draft,
        plan_id="plain-recovery-next",
        refs=tuple(
            replace(
                ref,
                plan_id="plain-recovery-next",
                source_ref=source,
                ref_id="new-plain-ref",
            )
            if ref.ref_id == "plain-ref"
            else replace(ref, plan_id="plain-recovery-next")
            if ref.ref_type == "request_only"
            else ref
            for ref in draft.refs
        ),
    )
    with RolloutCheckpointSaver(saver._storage.sessions_dir) as restarted:
        next_draft = register_projection_draft(restarted, session_id, next_draft)
        arguments = {
            "seal_idempotency_key": f"seal:{next_draft.plan_id}",
            "turn_id": "turn-1",
            "execution_id": restarted.execution_for_turn(session_id, turn_id="turn-1"),
            "provider_version": "contract-provider",
        }
        if change_identity:
            with pytest.raises(
                DetailUnavailableError, match="detail-unavailable.*new-plain-ref"
            ):
                restarted.seal_context_plan(session_id, next_draft, **arguments)
            return
        second = restarted.seal_context_plan(session_id, next_draft, **arguments)
        entry = second.selection[2]
        assert entry.ref.source_ref == first.selection[2].detail_ref
        assert entry.detail_ref != entry.ref.source_ref
        entry.detail_ref.require_owner(session_id, second.assembly_id)
        assert (
            _project(restarted, session_id, first.assembly_id)["native"]["request"]
            == _project(restarted, session_id, second.assembly_id)["native"]["request"]
        )
    # 再次打开 owner：source_ref 仍是旧 source，恢复只能读新 selection detail。
    with RolloutCheckpointSaver(saver._storage.sessions_dir) as restarted:
        assert (
            _project(restarted, session_id, second.assembly_id)["native"]["selection"][
                2
            ]["detail_ref"]
            == entry.detail_ref.to_dict()
        )


def test_sealed_detail_shape_is_typed_and_not_part_of_context_ref(
    projection_saver, projection_plan
) -> None:
    saver, session_id, session = projection_saver
    snapshot = projection_plan
    with sqlite3.connect(session / "rollout/index.sqlite") as connection:
        rows = connection.execute(
            "SELECT detail_ref FROM context_plan_details WHERE assembly_id = ?",
            (snapshot.assembly_id,),
        ).fetchall()
    refs = {detail_ref_from_key(row[0]) for row in rows}
    included = [entry for entry in snapshot.selection if entry.detail_ref is not None]
    assert refs == {entry.detail_ref for entry in included}
    assert len(included) == 2
    for entry in included:
        assert not hasattr(entry.ref, "detail_ref")
        assert "detail_ref" not in entry.ref.to_dict()
        ref = entry.detail_ref
        ref.require_owner(session_id, snapshot.assembly_id)
        assert set(ref.to_dict()) == {"session_id", "assembly_id", "detail_id"}
        path = session / detail_relative_path(ref)
        assert path.name == ref.detail_id and path.suffix == ""
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["detail_ref"] == ref.to_dict()
        assert not {"gc_after", "content_length"} & payload.keys()
        record = saver._storage.get_context_plan_detail(session_id, detail_ref=ref)
        assert record["detail_kind"] == "request_source"
        assert record["retention_class"] == "request_replay"
        assert record["visibility"] == entry.visibility
        assert record["length"] == entry.content_length
        assert record["detail_id"] == ref.detail_id
        assert record["detail_ref"] == ref
        assert "expires_at" in record


@pytest.mark.parametrize(
    "shape", ["bare_id", "physical_path", "dict", "foreign_session"]
)
def test_detail_read_rejects_wrong_owner_or_untyped_input_before_storage(
    projection_saver, projection_plan, monkeypatch, shape
) -> None:
    saver, session_id, session = projection_saver
    detail = projection_plan.selection[1].detail_ref
    invalid = {
        "bare_id": detail.detail_id,
        "physical_path": str(session / detail_relative_path(detail)),
        "dict": detail.to_dict(),
        "foreign_session": replace(detail, session_id="ses-foreign-detail"),
    }[shape]
    calls = []

    def reject_lookup(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("detail owner/shape 必须在 registry lookup 前校验")

    with monkeypatch.context() as patch:
        patch.setattr(saver._storage, "get_context_plan_detail", reject_lookup)
        with pytest.raises((TypeError, ValueError), match="source-mismatch"):
            saver.read_context_plan_detail(session_id, detail_ref=invalid)
    assert calls == []


@pytest.mark.parametrize("owner_field", ["session_id", "assembly_id", "detail_id"])
def test_corrupt_typed_selection_binding_is_rejected_after_restart(
    projection_saver, projection_plan, owner_field
) -> None:
    saver, session_id, session = projection_saver
    original = projection_plan.selection[1].detail_ref
    changed = replace(original, **{owner_field: f"wrong-{owner_field}"})
    with sqlite3.connect(session / "rollout/index.sqlite") as connection:
        connection.execute(
            "UPDATE context_assembly_selections SET detail_ref = ? "
            "WHERE assembly_id = ? AND plan_ordinal = 1",
            (detail_ref_key(changed), projection_plan.assembly_id),
        )
    with RolloutCheckpointSaver(saver._storage.sessions_dir) as restarted:
        for projector in (
            restarted.project_context_plan_to_messages,
            restarted.project_context_plan_to_native,
            restarted.project_context_plan_to_history,
        ):
            with pytest.raises(
                (RuntimeError, ValueError), match="mismatch|不一致|detail"
            ):
                projector(session_id, projection_plan.as_sealed_plan())


@pytest.mark.parametrize("port", ["seal", "dispatch", "prepare"])
def test_seal_failure_tombstones_source_details_before_removing_files(
    projection_saver, monkeypatch, port
) -> None:
    saver, session_id, session = projection_saver
    from app.domain.itemized.hashing import contribution_content_hash
    from app.domain.itemized.request_plan import ContextContribution

    body = [{"type": "text", "text": "typed cleanup source"}]
    contribution = ContextContribution(
        contribution_id="cleanup-source",
        source_kind="prompt",
        source_revision="source-v1",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
    )
    saver.register_context_contribution(session_id, contribution, request_content=body)
    removed = []
    original_remove = saver._detail_store.remove

    def fail_seal(*args, **kwargs):
        raise RuntimeError("injected assembly commit failure")

    def check_tombstone(*, session_id, record):
        with sqlite3.connect(session / "rollout/index.sqlite") as connection:
            row = connection.execute(
                "SELECT status, availability FROM context_plan_details WHERE detail_ref = ?",
                (detail_ref_key(record.detail_ref),),
            ).fetchone()
        assert row == ("unavailable", "unavailable")
        removed.append(record.detail_ref)
        return original_remove(session_id=session_id, record=record)

    with monkeypatch.context() as patch:
        patch.setattr(saver._storage, "seal_context_assembly", fail_seal)
        patch.setattr(saver._detail_store, "remove", check_tombstone)
        arguments = {
            "turn_id": "turn-1",
            "provider_version": "typed-contract",
            "execution_id": saver.execution_for_turn(session_id, turn_id="turn-1"),
        }
        with pytest.raises(RuntimeError, match="injected assembly commit failure"):
            if port == "seal":
                draft = saver.compose_committed_context_plan(
                    session_id, plan_id="cleanup-plan"
                )
                draft = register_projection_draft(saver, session_id, draft)
                saver.seal_context_plan(
                    session_id,
                    draft,
                    seal_idempotency_key=f"seal:{draft.plan_id}",
                    **arguments,
                )
            elif port == "dispatch":
                saver.seal_context_for_dispatch(
                    session_id, model_call_id="cleanup-call", **arguments
                )
            else:
                saver.prepare_context_for_provider(
                    session_id,
                    plan_creation_idempotency_key="create:cleanup-prepare",
                    seal_idempotency_key="seal:cleanup-prepare",
                    turn_id="turn-1",
                    provider_version="typed-contract",
                    target_format="responses",
                    prompt_contributions=(contribution,),
                )
    assert len(removed) == 1
    assert not (session / detail_relative_path(removed[0])).exists()
    assert saver.list_context_assemblies(session_id) == ()
