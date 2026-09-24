"""真实 runtime 来源清单经两次 full-copy 和重启保留 omitted binding，不读取正文。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_hash import context_request_hash
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    json_text,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    _artifacts,
)
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    fork_workspace as fork_workspace,  # noqa: PLC0414 - 当前正式文件独占工作区
)

SOURCE_SESSION_ID = "ses_e6d2707870e54cab8c135193c0802532"
TARGET_SESSION_ID = "ses_58a5607fd562454a932d851c95b73cc4"
GRANDCHILD_SESSION_ID = "ses_5ce2590d35c74fd9a71e8d7526be328c"


@pytest.fixture
def forbid_detail_body(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("omitted 来源清单复制和恢复不得读取或创建 detail 正文")

    monkeypatch.setattr(ContextPlanDetailStore, "read", forbidden)
    monkeypatch.setattr(ContextPlanDetailStore, "write", forbidden)


@pytest.fixture
def omitted_source(fork_workspace, session_bundle_factory, forbid_detail_body, request):
    sessions = fork_workspace / ".boxteam" / "sessions"
    for session in (SOURCE_SESSION_ID, TARGET_SESSION_ID, GRANDCHILD_SESSION_ID):
        session_bundle_factory(sessions, session)
    with RolloutCheckpointSaver(sessions) as saver:
        accepted = saver.accept_turn(
            SOURCE_SESSION_ID, accepted_ingress_id="source-ingress",
            acceptance_idempotency_key="source-root", payload="真实来源 root", payload_kind="text",
        )
        mode = getattr(request, "param", "alias")
        role = mode.removeprefix("overlay-") if mode.startswith("overlay-") else "none"
        contribution_id = "omitted-ref" if mode == "direct" else "known-contribution"
        metadata = {"source_ref": "registered-alias"}
        if role != "none":
            metadata.update(overlay_ref="omitted-ref", overlay_role=role, source_overlay_epoch=0)
        kind = "prompt" if role == "none" else f"overlay_{role}"
        unavailable = ContextContribution(
            contribution_id=contribution_id, source_kind="environment", source_revision="revision-1",
            content_hash=contribution_content_hash(kind, "not-materialized"),
            content_length=len(canonical_json_bytes("not-materialized")),
            contribution_kind=kind, metadata=metadata, source_ordinal=0,
        )
        ref = ContextRef.request_only_ref(
            "omitted-ref", session_id=SOURCE_SESSION_ID, thread_id="thread-1", plan_id="actual-runtime-plan",
            source_revision=unavailable.source_revision,
            content_hash_value=unavailable.content_hash, content_length=unavailable.content_length,
            availability="unavailable", source_ref="registered-alias", base_delta_role=role,
            source_overlay_epoch=0 if role != "none" else None,
        )
        draft = ContextRequestPlan(
            session_id=SOURCE_SESSION_ID, plan_id="actual-runtime-plan", refs=(ref,), contributions=(unavailable,),
            plan_creation_idempotency_key="actual-runtime-create",
        )
        saver.create_context_plan(SOURCE_SESSION_ID, draft)
        snapshot = ContextPlanComposer().assembly(
            plan=draft, session_id=SOURCE_SESSION_ID, assembly_id="omitted-assembly",
            turn_id=accepted["turn_id"], execution_id=accepted["initial_execution_id"],
            provider_version="fork-import-source-fixture",
        )
        # Composer 默认省略 binding；此处显式保留已经真实注册的合法来源身份。
        snapshot = replace(
            snapshot, selection=(replace(snapshot.selection[0], contribution_id=contribution_id),),
        )
        sealed = snapshot.as_sealed_plan()
        snapshot = replace(
            snapshot, plan_hash=sealed.plan_hash(),
            request_hash=context_request_hash(
                sealed, snapshot.provider_version, projector_id=snapshot.projector_id,
                projector_version=snapshot.projector_version, target_format=snapshot.target_format,
                wire_request=snapshot.request_hash_preimage,
            ),
        )
        saver.seal_context_assembly(
            snapshot, seal_idempotency_key="actual-runtime-seal",
            seal_input_hash=sha256_jcs({"fixture": "registered omitted source"}),
        )
        with saver._storage._connect(SOURCE_SESSION_ID, "", read_only=True) as connection:
            assert connection.execute("SELECT count(*) FROM context_contributions").fetchone() == (0,)
            assert connection.execute("SELECT count(*) FROM context_plan_details").fetchone() == (0,)
        yield saver, snapshot, mode


def _only_registration(saver, session):
    with saver._storage._connect(session, "", read_only=True) as connection:
        plan_id = connection.execute("SELECT plan_id FROM context_plans").fetchone()[0]
    return saver.get_context_plan_registration(session, plan_id=plan_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("omitted_source", ["direct", "alias", "overlay-base", "overlay-delta"], indirect=True)
async def test_full_copy_omitted_existing_source_survives_recursive_import_and_restart(omitted_source):
    saver, original, mode = omitted_source
    source_root = saver._storage.root(SOURCE_SESSION_ID)
    source_before = _artifacts(source_root)
    await saver.afork(source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID, mode="full_rollout_copy")
    assert _artifacts(source_root) == source_before
    target = _only_registration(saver, TARGET_SESSION_ID)
    assert target.registration_origin == "fork_import" and target.draft is None
    assert target.source_manifest["contributions"][0]["contribution_id"] != original.selection[0].contribution_id
    target_before = _artifacts(saver._storage.root(TARGET_SESSION_ID))
    await saver.afork(source_session_id=TARGET_SESSION_ID, target_session_id=GRANDCHILD_SESSION_ID, mode="full_rollout_copy")
    assert _artifacts(saver._storage.root(TARGET_SESSION_ID)) == target_before
    with RolloutCheckpointSaver(saver._storage.sessions_dir) as restarted:
        for session in (TARGET_SESSION_ID, GRANDCHILD_SESSION_ID):
            restored = _only_registration(restarted, session)
            manifest = restored.source_manifest
            copied = restarted.get_context_assembly(session, assembly_id=restored.assembly_id)
            assert manifest["session_id"] == session and manifest["plan_id"] == copied.plan_id
            assert copied.plan_id != original.plan_id and copied.assembly_id != original.assembly_id
            assert copied.contributions == () and restored.draft is None
            assert len(manifest["contributions"]) == 1
            contribution = manifest["contributions"][0]
            entry, ref = copied.selection[0], manifest["refs"][0]
            assert entry.contribution_id == contribution["contribution_id"]
            assert not entry.included and entry.contribution_ordinal is None and entry.detail_ref is None
            assert contribution["body"] is contribution["assembly_id"] is contribution["contribution_ordinal"] is None
            assert ref["session_id"] == session and ref["plan_id"] == copied.plan_id
            assert ref["ref_id"] != "omitted-ref" and ref["source_ref"] != "registered-alias"
            assert ref["source_ref"] == contribution["metadata"]["source_ref"]
            if mode.startswith("overlay-"):
                assert contribution["metadata"]["overlay_ref"] == ref["ref_id"]
                assert contribution["metadata"]["source_overlay_epoch"] == ref["source_overlay_epoch"]
            with restarted._storage._connect(session, "", read_only=True) as connection:
                assert connection.execute("SELECT count(*) FROM context_contributions").fetchone() == (0,)
                assert connection.execute("SELECT count(*) FROM context_plan_details").fetchone() == (0,)
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
                rows = connection.execute("SELECT manifest_json FROM context_plan_contributions").fetchall()
                assert [json.loads(row[0]) for row in rows] == manifest["contributions"]
    assert _artifacts(source_root) == source_before


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["manifest", "index", "wrong_ref"])
async def test_recursive_fork_rejects_import_evidence_tampering_before_publication(omitted_source, mutation):
    saver = omitted_source[0]
    await saver.afork(source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID, mode="full_rollout_copy")
    with saver._storage._connect(TARGET_SESSION_ID, "") as connection:
        if mutation == "index":
            connection.execute("DELETE FROM context_plan_contributions")
        else:
            raw = connection.execute("SELECT source_manifest_json FROM context_plans").fetchone()[0]
            manifest = json.loads(raw)
            if mutation == "manifest":
                manifest["contributions"][0]["source_ordinal"] += 1
            else:
                manifest["contributions"][0]["metadata"]["source_ref"] = "unknown-source"
            connection.execute("UPDATE context_plans SET source_manifest_json=?", (json_text(manifest),))
    target_before = _artifacts(saver._storage.root(TARGET_SESSION_ID))
    with pytest.raises(ValueError, match="source-mismatch|plan-order-integrity"):
        await saver.afork(source_session_id=TARGET_SESSION_ID, target_session_id=GRANDCHILD_SESSION_ID, mode="full_rollout_copy")
    assert not saver._storage.root(GRANDCHILD_SESSION_ID).exists()
    assert _artifacts(saver._storage.root(TARGET_SESSION_ID)) == target_before
