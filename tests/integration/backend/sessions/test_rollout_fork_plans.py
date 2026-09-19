"""真实 Saver 的 sealed plan/detail/tool-set 跨会话复制验收。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import contribution_content_hash, sha256_jcs
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import ContextContribution
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork.remap_prepare import (
    prepare_full_copy_remap,
)
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    detail_relative_path,
)
from tests.integration.backend.sessions.itemized_migration_helpers import (
    prepare_migration_workspace,
)

SOURCE_SESSION_ID = "ses_e6d2707870e54cab8c135193c0802532"
TARGET_SESSION_ID = "ses_58a5607fd562454a932d851c95b73cc4"
COPY_TARGET_SESSION_ID = "ses_5ce2590d35c74fd9a71e8d7526be328c"


@pytest.fixture
def plan_source(request: pytest.FixtureRequest, session_bundle_factory):
    sessions = prepare_migration_workspace(request) / ".boxteam" / "sessions"
    for session in (SOURCE_SESSION_ID, TARGET_SESSION_ID):
        session_bundle_factory(sessions, session)
    with RolloutCheckpointSaver(sessions) as saver:
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "source-checkpoint"
        checkpoint["channel_values"] = {
            "messages": [
                HumanMessage(
                    content="source 输入不能文本替换",
                    id="source-user",
                    response_metadata={"turn_id": "source-turn"},
                ),
                AIMessage(content="source 输出不能文本替换", id="source-final"),
            ]
        }
        checkpoint["channel_versions"] = {"messages": "1"}
        saver.put(
            build_checkpoint_config(SOURCE_SESSION_ID),
            checkpoint,
            {"source": "integration"},
            {"messages": "1"},
        )
        body = [{"type": "text", "text": "source plan 明文不是 identity，必须保持"}]
        saver.register_context_contribution(
            SOURCE_SESSION_ID,
            ContextContribution(
                contribution_id="source-contribution",
                source_kind="environment",
                source_revision="source-revision",
                body=body,
                content_hash=contribution_content_hash("prompt", body),
                metadata={"source_ref": "source-contribution"},
            ),
            request_content=body,
        )
        plan = saver.compose_committed_context_plan(
            SOURCE_SESSION_ID,
            plan_id="source-plan",
            tool_snapshot=(
                {
                    "tool_id": "echo",
                    "name": "echo",
                    "parameters": {
                        "type": "object",
                        "properties": {"source": {"type": "string"}},
                    },
                },
            ),
        )
        plan = replace(
            plan,
            plan_creation_idempotency_key="test_rollout_fork_plans:source-plan:create",
        )
        saver.create_context_plan(SOURCE_SESSION_ID, plan)
        sealed = saver.seal_context_plan(
            SOURCE_SESSION_ID,
            plan,
            turn_id="source-turn",
            execution_id=saver.execution_for_turn(SOURCE_SESSION_ID, turn_id="source-turn"),
            provider_version="integration-provider",
            seal_idempotency_key="test_rollout_fork_plans:source-plan:seal",
        )
        yield saver, sealed, body


def test_full_copy_sealed_sources_restore_from_target_only(plan_source) -> None:
    saver, sealed, body = plan_source
    storage = saver._storage
    source_root = storage.root(SOURCE_SESSION_ID)
    before = artifact_manifest(source_root)
    source_view = storage.clone_rollout(
        source_thread_id=SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID, source_checkpoint_id=None
    )
    fields = {
        "source_session_id": SOURCE_SESSION_ID,
        "target_session_id": TARGET_SESSION_ID,
        "source_checkpoint_id": None,
        "source_view_id": source_view,
        "fork_mode": "full_rollout_copy",
        "relationship": "detached",
    }
    materialization, fork_id = storage.begin_fork_materialization(**fields)
    storage.commit_fork_materialization(materialization, **fields)
    assert artifact_manifest(source_root) == before
    with storage._connect(TARGET_SESSION_ID, "", read_only=True) as connection:
        target_assembly = connection.execute(
            "SELECT target_local_id FROM fork_identity_mappings WHERE fork_id=? AND entity_type='assembly' AND source_local_id=?",
            (fork_id, sealed.assembly_id),
        ).fetchone()[0]
        target_plan_id = connection.execute(
            "SELECT target_local_id FROM fork_identity_mappings WHERE fork_id=? AND entity_type='plan' AND source_local_id='source-plan'",
            (fork_id,),
        ).fetchone()[0]
    source_root.rename(source_root.with_name("rollout-archived"))
    with RolloutCheckpointSaver(storage.sessions_dir) as restarted:
        snapshot = restarted.get_context_assembly(TARGET_SESSION_ID, assembly_id=target_assembly)
        assert snapshot.session_id == TARGET_SESSION_ID
        target_plan = snapshot.as_sealed_plan()
        assert target_plan.plan_id == target_plan_id != sealed.plan_id
        assert all(ref.session_id == TARGET_SESSION_ID for ref in target_plan.refs)
        assert all(
            ref.plan_id == target_plan_id
            for ref in target_plan.refs
            if ref.ref_type == "request_only"
        )
        assert all(
            ref.session_id == TARGET_SESSION_ID and ref.plan_id == target_plan_id
            for ref in target_plan.tool_set_refs
        )
        assert all(entry.ref.session_id == TARGET_SESSION_ID for entry in target_plan.selection)
        assert all("detail_ref" not in ref.to_dict() for ref in target_plan.refs)
        for entry in target_plan.selection:
            if entry.included and entry.ref.ref_type == "request_only":
                assert isinstance(entry.detail_ref, DetailRef)
                entry.detail_ref.require_owner(TARGET_SESSION_ID, target_assembly)
                manifest = restarted._storage.get_context_plan_detail(
                    TARGET_SESSION_ID, detail_ref=entry.detail_ref
                )
                assert manifest["detail_ref"] == entry.detail_ref
                assert manifest["detail_id"] == entry.detail_ref.detail_id
                assert manifest["relative_path"] == (
                    f"rollout/context-plan-details/{target_assembly}/{entry.detail_ref.detail_id}"
                )
        projected, losses = restarted.project_context_plan_with_diagnostics(
            TARGET_SESSION_ID, target_plan
        )
        assert losses == () or losses == []
        rendered = json.dumps(
            [message.model_dump(mode="json") for message in projected],
            ensure_ascii=False,
        )
        assert body[0]["text"] in rendered
        assert "source 输入不能文本替换" in rendered
        assert "source 输出不能文本替换" in rendered
        assert {ref.ref_id for ref in target_plan.refs}.isdisjoint(
            ref.ref_id for ref in sealed.refs
        )
        assert {ref.ref_id for ref in target_plan.tool_set_refs}.isdisjoint(
            ref.ref_id for ref in sealed.tool_set_refs
        )
        native = restarted.project_context_plan_to_native(TARGET_SESSION_ID, target_plan)
        assert native is not None


@pytest.mark.parametrize(
    "corruption",
    [
        "ref_owner",
        "missing_manifest",
        "source_schema",
        "detail_hash",
        "detail_missing",
        "committed_offset",
        "unpublished",
    ],
)
def test_full_copy_rejects_invalid_source_without_rebuilding_it(
    plan_source, corruption: str
) -> None:
    saver, sealed, _ = plan_source
    storage = saver._storage
    source_root = storage.root(SOURCE_SESSION_ID)
    with (
        closing(sqlite3.connect(storage.index_path(SOURCE_SESSION_ID))) as connection,
        connection,
    ):
        if corruption == "ref_owner":
            raw = json.loads(
                connection.execute(
                    "SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?",
                    (sealed.assembly_id,),
                ).fetchone()[0]
            )
            raw["refs"][0]["session_id"] = "unrelated-session"
            connection.execute(
                "UPDATE context_assemblies SET snapshot_json=? WHERE assembly_id=?",
                (json.dumps(raw), sealed.assembly_id),
            )
        elif corruption == "missing_manifest":
            connection.execute(
                "DELETE FROM assembly_item_refs WHERE assembly_id=? AND ref_ordinal=0",
                (sealed.assembly_id,),
            )
        elif corruption == "source_schema":
            connection.execute("UPDATE database_meta SET schema_version=1")
        elif corruption == "committed_offset":
            connection.execute("UPDATE database_meta SET committed_jsonl_offset=0")
        elif corruption == "unpublished":
            connection.execute("UPDATE database_meta SET database_state='migrating'")
        else:
            relative = connection.execute(
                "SELECT relative_path FROM context_plan_details WHERE assembly_id=?",
                (sealed.assembly_id,),
            ).fetchone()[0]
            if corruption == "detail_missing":
                source_root.parent.joinpath(relative).unlink()
            else:
                source_root.parent.joinpath(relative).write_text(
                    '{"detail":"corrupted"}'
                )
    before = artifact_manifest(source_root)
    with pytest.raises((RuntimeError, ValueError)) as error:
        storage.clone_rollout(
            source_thread_id=SOURCE_SESSION_ID,
            target_thread_id=TARGET_SESSION_ID,
            source_checkpoint_id=None,
        )
    if corruption == "source_schema":
        assert "schema-upgrade-required" in str(error.value)
        assert "v1_migration_required" not in str(error.value)
    assert artifact_manifest(source_root) == before
    assert not storage.root(TARGET_SESSION_ID).exists()
    assert not (storage.root(TARGET_SESSION_ID).parent / "legacy-import").exists()


def test_full_copy_preflight_observes_wal_without_modifying_source(plan_source) -> None:
    saver, _, _ = plan_source
    storage = saver._storage
    source = storage.root(SOURCE_SESSION_ID)
    with closing(sqlite3.connect(storage.index_path(SOURCE_SESSION_ID))) as connection:
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE database_meta SET schema_version=1")
        connection.commit()
        assert (source / "index.sqlite-wal").stat().st_size > 0
        before = artifact_manifest(source)
        with pytest.raises(RuntimeError, match="schema-upgrade-required"):
            storage.clone_rollout(
                source_thread_id=SOURCE_SESSION_ID,
                target_thread_id=TARGET_SESSION_ID,
                source_checkpoint_id=None,
            )
        assert artifact_manifest(source) == before
        assert not storage.root(TARGET_SESSION_ID).exists()


@pytest.mark.parametrize("failure_phase", ["during_detail", "after_detail"])
def test_failed_full_copy_restores_jsonl_and_detail_bytes(
    plan_source, monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    from app.services.infrastructure.rollout_context.fork import remap, remap_files

    saver, _, _ = plan_source
    storage = saver._storage
    source_before = artifact_manifest(storage.root(SOURCE_SESSION_ID))
    source_view = storage.clone_rollout(
        source_thread_id=SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID, source_checkpoint_id=None
    )
    fields = {
        "source_session_id": SOURCE_SESSION_ID,
        "target_session_id": TARGET_SESSION_ID,
        "source_checkpoint_id": None,
        "source_view_id": source_view,
        "fork_mode": "full_rollout_copy",
        "relationship": "detached",
    }
    materialization, _ = storage.begin_fork_materialization(**fields)
    target_root = storage.root(TARGET_SESSION_ID)
    before = artifact_manifest(target_root)

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected detail materialization failure")

    if failure_phase == "during_detail":
        monkeypatch.setattr(remap_files, "canonical_json_bytes", fail)
    else:
        monkeypatch.setattr(remap, "rewrite_full_copy_fast_columns", fail)
    with pytest.raises(RuntimeError, match="injected detail materialization failure"):
        storage.commit_fork_materialization(materialization, **fields)
    assert artifact_manifest(target_root) == before
    assert artifact_manifest(storage.root(SOURCE_SESSION_ID)) == source_before


@pytest.fixture
def typed_detail_mapping_state(request: pytest.FixtureRequest, session_bundle_factory):
    """真实 schema3 registry + fork/domain 映射；这些 optional manifest 无可用正文。"""
    sessions = prepare_migration_workspace(request) / ".boxteam" / "sessions"
    for session in (SOURCE_SESSION_ID, TARGET_SESSION_ID):
        session_bundle_factory(sessions, session)
    with RolloutCheckpointSaver(sessions) as saver:
        storage = saver._storage
        storage.initialize(SOURCE_SESSION_ID)
        storage.initialize(TARGET_SESSION_ID)
        refs = tuple(
            DetailRef(SOURCE_SESSION_ID, assembly, leaf)
            for assembly, leaf in (
                ("assembly-a", "same-leaf"),
                ("assembly-a", "another-leaf"),
                ("assembly-b", "same-leaf"),
            )
        )
        for ref in refs:
            relative = detail_relative_path(ref)
            (storage.root(SOURCE_SESSION_ID).parent / relative).parent.mkdir(
                parents=True, exist_ok=True
            )
            storage.register_context_plan_detail(
                DetailRecord(
                    session_id=ref.session_id,
                    assembly_id=ref.assembly_id,
                    detail_id=ref.detail_id,
                    detail_kind="request_source",
                    retention_class="request_replay",
                    visibility="internal",
                    relative_path=relative.as_posix(),
                    content_hash=sha256_jcs({}),
                    length=0,
                    source_revision="source-revision",
                    required=False,
                    sensitive=False,
                    status="unavailable",
                    availability="unavailable",
                )
            )
        with storage._connect(SOURCE_SESSION_ID, "", read_only=True) as connection:
            state = prepare_full_copy_remap(
                storage,
                connection,
                source_session_id=SOURCE_SESSION_ID,
                target_session_id=TARGET_SESSION_ID,
                fork_id="typed-detail-fork",
                checkpoint_ns="",
                timestamp="2026-09-08T00:00:00Z",
            )
            assert state is not None
            yield refs, state


def test_typed_detail_mapping_keeps_leaf_owner_and_selection_separate(
    typed_detail_mapping_state,
) -> None:
    refs, state = typed_detail_mapping_state
    targets = [
        detail_ref_from_key(state.maps["detail"][detail_ref_key(ref)]) for ref in refs
    ]
    assert len(set(targets)) == len(refs)
    assert len({ref.detail_id for ref in targets}) == len(refs)
    for source, target in zip(refs, targets, strict=True):
        target.require_owner(TARGET_SESSION_ID, state.maps["assembly"][source.assembly_id])
        assert target.detail_id != source.detail_id
    source = refs[0]
    raw_ref = ContextRef.request_only_ref(
        "source-plan-item",
        session_id=SOURCE_SESSION_ID,
        plan_id="source-plan",
        source_revision="source-revision",
        source_ref=source,
        content_length=0,
        content_hash_value=sha256_jcs({}),
        availability="unavailable",
    ).to_dict()
    value = {
        "refs": [raw_ref],
        "selection": [
            {
                "assembly_id": source.assembly_id,
                "detail_ref": source.to_dict(),
                "ref": raw_ref,
            }
        ],
        "source": {"detail_ref": source.to_dict()},
        "detail_lineage": {
            "source_detail_ref": source.to_dict(),
            "target_detail_ref": source.to_dict(),
        },
    }
    mapped = state.remap_json(value)
    assert "detail_ref" not in mapped["refs"][0]
    assert mapped["refs"][0]["source_ref"] == targets[0].to_dict()
    assert mapped["selection"][0]["detail_ref"] == targets[0].to_dict()
    assert mapped["source"] == value["source"]
    assert mapped["detail_lineage"] == {
        "source_detail_ref": source.to_dict(),
        "target_detail_ref": targets[0].to_dict(),
    }


@pytest.mark.parametrize(
    "corruption",
    ["bare_id", "foreign_owner", "unknown_detail", "missing_leaf", "raw_final_detail"],
)
def test_typed_detail_mapping_refuses_legacy_or_unowned_refs(
    typed_detail_mapping_state, corruption: str
) -> None:
    refs, state = typed_detail_mapping_state
    value = {"detail_ref": refs[0].to_dict()}
    if corruption == "bare_id":
        value["detail_ref"] = refs[0].detail_id
    elif corruption == "foreign_owner":
        value["detail_ref"]["session_id"] = "foreign"
    elif corruption == "unknown_detail":
        value["detail_ref"]["detail_id"] = "unregistered"
    elif corruption == "missing_leaf":
        value["detail_ref"].pop("detail_id")
    else:
        value["ref_type"] = "request_only"
    with pytest.raises((TypeError, ValueError, RuntimeError)):
        state.remap_json(value)


def test_full_copy_unavailable_detail_manifest_localizes_typed_owner(
    typed_detail_mapping_state, session_bundle_factory
) -> None:
    refs, state = typed_detail_mapping_state
    storage = state.service
    session_bundle_factory(storage.sessions_dir, COPY_TARGET_SESSION_ID)
    source_before = artifact_manifest(storage.root(SOURCE_SESSION_ID))
    source_view = storage.clone_rollout(
        source_thread_id=SOURCE_SESSION_ID,
        target_thread_id=COPY_TARGET_SESSION_ID,
        source_checkpoint_id=None,
    )
    fields = {
        "source_session_id": SOURCE_SESSION_ID,
        "target_session_id": COPY_TARGET_SESSION_ID,
        "source_checkpoint_id": None,
        "source_view_id": source_view,
        "fork_mode": "full_rollout_copy",
        "relationship": "detached",
    }
    materialization, fork_id = storage.begin_fork_materialization(**fields)
    storage.commit_fork_materialization(materialization, **fields)
    for source_ref in refs:
        key = storage.fork_target_identity(
            COPY_TARGET_SESSION_ID,
            fork_id=fork_id,
            source_session_id=SOURCE_SESSION_ID,
            entity_type="detail",
            source_local_id=detail_ref_key(source_ref),
        )
        target_ref = detail_ref_from_key(key)
        target_ref.require_owner(COPY_TARGET_SESSION_ID)
        manifest = storage.get_context_plan_detail(COPY_TARGET_SESSION_ID, detail_ref=target_ref)
        assert manifest["detail_id"] == target_ref.detail_id != source_ref.detail_id
        assert manifest["status"] == manifest["availability"] == "unavailable"
        assert manifest["relative_path"] == detail_relative_path(target_ref).as_posix()
        with pytest.raises(ValueError, match="source-mismatch"):
            storage.get_context_plan_detail(COPY_TARGET_SESSION_ID, detail_ref=source_ref)
    assert artifact_manifest(storage.root(SOURCE_SESSION_ID)) == source_before


def test_full_copy_preserves_distinct_source_detail_and_sealed_detail(
    plan_source,
) -> None:
    saver, initial, _ = plan_source
    storage = saver._storage
    body = [{"type": "text", "text": "已有 typed source detail 正文不可丢失"}]
    source_detail: DetailRef | None = None
    for ordinal in (1, 2):
        ref_id, plan_id = f"source-extra-ref-{ordinal}", f"source-extra-plan-{ordinal}"
        ref = ContextRef.request_only_ref(
            ref_id,
            session_id=SOURCE_SESSION_ID,
            plan_id=plan_id,
            source_revision="existing-source-revision",
            content=body,
            source_ref=source_detail
            if source_detail is not None
            else "original-source",
            visibility="internal",
        )
        plan = saver.compose_context_plan(
            session_id=SOURCE_SESSION_ID,
            plan_id=plan_id,
            refs=(ref,),
            history_view_revision=initial.history_view_revision,
        )
        plan = replace(
            plan,
            plan_creation_idempotency_key=f"test_rollout_fork_plans:{plan_id}:create",
        )
        saver.create_context_plan(SOURCE_SESSION_ID, plan)
        sealed = saver.seal_context_plan(
            SOURCE_SESSION_ID,
            plan,
            turn_id="source-turn",
            execution_id=saver.execution_for_turn(SOURCE_SESSION_ID, turn_id="source-turn"),
            provider_version="integration-provider",
            seal_idempotency_key=f"test_rollout_fork_plans:{plan_id}:seal",
            request_only_content={ref_id: body} if ordinal == 1 else None,
        )
        entry = next(entry for entry in sealed.selection if entry.ref.ref_id == ref_id)
        assert isinstance(entry.detail_ref, DetailRef)
        if ordinal == 1:
            source_detail = entry.detail_ref
        else:
            assert entry.ref.source_ref == source_detail != entry.detail_ref
    source_root = storage.root(SOURCE_SESSION_ID)
    before = artifact_manifest(source_root)
    source_view = storage.clone_rollout(
        source_thread_id=SOURCE_SESSION_ID, target_thread_id=TARGET_SESSION_ID, source_checkpoint_id=None
    )
    fields = {
        "source_session_id": SOURCE_SESSION_ID,
        "target_session_id": TARGET_SESSION_ID,
        "source_checkpoint_id": None,
        "source_view_id": source_view,
        "fork_mode": "full_rollout_copy",
        "relationship": "detached",
    }
    materialization, fork_id = storage.begin_fork_materialization(**fields)
    storage.commit_fork_materialization(materialization, **fields)
    target_assembly = storage.fork_target_identity(
        TARGET_SESSION_ID,
        fork_id=fork_id,
        source_session_id=SOURCE_SESSION_ID,
        entity_type="assembly",
        source_local_id=sealed.assembly_id,
    )
    assert artifact_manifest(source_root) == before
    source_root.rename(source_root.with_name("rollout-archived"))
    with RolloutCheckpointSaver(storage.sessions_dir) as restarted:
        target = restarted.get_context_assembly(TARGET_SESSION_ID, assembly_id=target_assembly)
        entry = next(
            entry
            for entry in target.selection
            if isinstance(entry.ref.source_ref, DetailRef)
        )
        entry.detail_ref.require_owner(TARGET_SESSION_ID, target_assembly)
        entry.ref.source_ref.require_owner(TARGET_SESSION_ID)
        assert entry.ref.source_ref.assembly_id != entry.detail_ref.assembly_id
        assert "detail_ref" not in entry.ref.to_dict()
        assert (
            restarted.read_context_plan_detail(
                TARGET_SESSION_ID, detail_ref=entry.ref.source_ref
            )["detail"]
            == body
        )
        projected, losses = restarted.project_context_plan_with_diagnostics(
            TARGET_SESSION_ID, target.as_sealed_plan()
        )
        assert not losses
        assert body[0]["text"] in json.dumps(
            [message.model_dump(mode="json") for message in projected],
            ensure_ascii=False,
        )
