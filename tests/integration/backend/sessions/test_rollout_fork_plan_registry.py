"""fresh schema4 的 full-copy registry、真实 draft 与显式 sealed import。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.domain.itemized.hashing import contribution_content_hash, sha256_jcs
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    draft_manifest,
    json_text,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    read_registration,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    _artifacts,
)
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    fork_workspace as fork_workspace,  # noqa: PLC0414 - 显式注入当前正式测试工作区
)
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    protected_key as protected_key,  # noqa: PLC0414 - 明确 key fixture
)
from tests.integration.backend.sessions.test_rollout_fork_protected import (
    protected_source as protected_source,  # noqa: PLC0414 - 不运行被导入文件
)

SOURCE_SESSION_ID = "ses_e6d2707870e54cab8c135193c0802532"
TARGET_SESSION_ID = "ses_58a5607fd562454a932d851c95b73cc4"


@pytest.fixture
def draft_factory(protected_source):
    saver = protected_source[0]

    def create(plan_id, *, tool_name="read_file"):
        tool = ToolSetRef.from_tool_snapshot(
            session_id=SOURCE_SESSION_ID,
            plan_id=plan_id,
            snapshot_id="same-local-tools",
            source_revision=f"{tool_name}-revision",
            tools=(
                {
                    "name": tool_name,
                    "parameters": {
                        "type": "object",
                        "properties": {"api_key": {"type": "string"}},
                    },
                },
            ),
        )
        plan = ContextRequestPlan(
            session_id=SOURCE_SESSION_ID,
            plan_id=plan_id,
            refs=(),
            tool_set_refs=(tool,),
            plan_creation_idempotency_key=f"create-{plan_id}",
        )
        saver.create_context_plan(SOURCE_SESSION_ID, plan)
        return plan

    return create


def _target_plan_id(connection, source_plan):
    return connection.execute(
        "SELECT target_local_id FROM fork_identity_mappings WHERE entity_type='plan' AND source_local_id=?",
        (source_plan,),
    ).fetchone()[0]


@pytest.mark.asyncio
async def test_full_copy_keeps_true_draft_revision_initial_and_nullable_tool_binding(
    protected_source, draft_factory, protected_key
):
    saver, sealed, _record, _body = protected_source
    initial = draft_factory("real-draft")
    current = replace(initial, tool_set_refs=(), history_view_revision=2)
    saver.revise_context_plan(SOURCE_SESSION_ID, current, expected_revision=0)
    source = saver._storage.root(SOURCE_SESSION_ID)
    before = _artifacts(source)
    await saver.afork(
        source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID, mode="full_rollout_copy"
    )
    assert _artifacts(source) == before
    with saver._storage._connect(TARGET_SESSION_ID, "", read_only=True) as connection:
        target_draft_id = _target_plan_id(connection, "real-draft")
        target_sealed_id = _target_plan_id(connection, sealed.plan_id)
        draft = read_registration(connection, TARGET_SESSION_ID, target_draft_id)
        imported = read_registration(connection, TARGET_SESSION_ID, target_sealed_id)
        assert draft.plan_state == "unsealed" and draft.registration_origin == "runtime"
        assert draft.revision == 1 and draft.assembly_id is None
        assert draft.seal_hash is None and draft.seal_input_hash is None
        assert draft.draft.session_id == TARGET_SESSION_ID and draft.draft.selection == ()
        assert (
            draft.draft.history_view_revision == 2 and draft.draft.tool_set_refs == ()
        )
        assert imported.registration_origin == "fork_import" and imported.draft is None
        assert imported.source_provenance == {
            "source_session_id": SOURCE_SESSION_ID,
            "source_plan_id": sealed.plan_id,
            "source_assembly_id": sealed.assembly_id,
            "source_snapshot_hash": sha256_jcs(sealed.to_dict()),
            "source_schema_version": 4,
            "audit_id": connection.execute(
                "SELECT fork_id FROM fork_origins"
            ).fetchone()[0],
        }
        assert imported.seal_input_hash is None
        raw_initial, raw_current = connection.execute(
            "SELECT creation_json, draft_json FROM context_plans WHERE plan_id=?",
            (target_draft_id,),
        ).fetchone()
        initial_manifest = json.loads(raw_initial)
        assert initial_manifest["tool_set_refs"][0]["assembly_id"] is None
        assert initial_manifest["tool_set_refs"][0]["session_id"] == TARGET_SESSION_ID
        assert initial_manifest["tool_set_refs"][0]["plan_id"] == target_draft_id
        assert initial_manifest["tool_set_refs"][0]["ref_id"] != "same-local-tools"
        assert (
            initial_manifest["plan_creation_idempotency_key"]
            != initial.plan_creation_idempotency_key
        )
        assert json.loads(raw_current) == draft_manifest(draft.draft)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    with RolloutCheckpointSaver(
        saver._storage.sessions_dir, protected_detail_key=protected_key
    ) as restarted:
        assert (
            restarted.get_context_plan_registration(TARGET_SESSION_ID, plan_id=target_draft_id)
            == draft
        )
        snapshot = restarted.get_context_assembly(
            TARGET_SESSION_ID, assembly_id=imported.assembly_id
        )
        assert snapshot.plan_id == target_sealed_id


@pytest.mark.asyncio
async def test_full_copy_same_tool_local_id_stays_scoped_to_each_draft(
    protected_source, draft_factory
):
    saver = protected_source[0]
    first = draft_factory("draft-a", tool_name="read_file")
    second = draft_factory("draft-b", tool_name="write_file")
    await saver.afork(
        source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID, mode="full_rollout_copy"
    )
    with saver._storage._connect(TARGET_SESSION_ID, "", read_only=True) as connection:
        targets = [
            read_registration(
                connection, TARGET_SESSION_ID, _target_plan_id(connection, source.plan_id)
            ).draft
            for source in (first, second)
        ]
        assert targets[0].plan_id != targets[1].plan_id
        for target, source in zip(targets, (first, second), strict=True):
            tool = target.tool_set_refs[0]
            assert tool.session_id == TARGET_SESSION_ID and tool.plan_id == target.plan_id
            assert tool.ref_id != source.tool_set_refs[0].ref_id
            assert tool.tools == source.tool_set_refs[0].tools
            assert tool.assembly_id is None
        assert connection.execute(
            "SELECT count(*) FROM tool_set_snapshots WHERE assembly_id IS NULL"
        ).fetchone() == (2,)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table", ["context_plan_refs", "context_plan_contributions", "tool_set_snapshots"]
)
async def test_source_registry_corruption_rejects_copy_before_publication(
    protected_source, draft_factory, table
):
    saver = protected_source[0]
    draft_factory("unsealed-with-tool")
    with saver._storage._connect(SOURCE_SESSION_ID, "") as connection:
        connection.execute(f"DELETE FROM {table}")
    source = saver._storage.root(SOURCE_SESSION_ID)
    before = _artifacts(source)
    with pytest.raises(ValueError, match="source-mismatch"):
        await saver.afork(
            source_session_id=SOURCE_SESSION_ID,
            target_session_id=TARGET_SESSION_ID,
            mode="full_rollout_copy",
        )
    assert _artifacts(source) == before
    assert not saver._storage.root(TARGET_SESSION_ID).exists()


@pytest.mark.asyncio
async def test_import_failure_leaves_source_and_target_unpublished(
    protected_source, monkeypatch
):
    from app.services.infrastructure.rollout_context.fork.full_copy import plans

    saver = protected_source[0]
    source = saver._storage.root(SOURCE_SESSION_ID)
    before = _artifacts(source)

    def reject(*args, **kwargs):
        raise RuntimeError("injected import transaction failure")

    monkeypatch.setattr(plans, "write_imported_registration", reject)
    with pytest.raises(RuntimeError, match="injected import"):
        await saver.afork(
            source_session_id=SOURCE_SESSION_ID,
            target_session_id=TARGET_SESSION_ID,
            mode="full_rollout_copy",
        )
    assert _artifacts(source) == before
    assert not saver._storage.root(TARGET_SESSION_ID).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["provenance", "source_audit"])
async def test_fork_source_fingerprint_is_verified_on_restore(
    protected_source, mutation
):
    saver, sealed, _record, _body = protected_source
    await saver.afork(
        source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID, mode="full_rollout_copy"
    )
    with saver._storage._connect(TARGET_SESSION_ID, "") as connection:
        target_plan = _target_plan_id(connection, sealed.plan_id)
        if mutation == "provenance":
            row = connection.execute(
                "SELECT source_provenance_json FROM context_plans WHERE plan_id=?",
                (target_plan,),
            ).fetchone()
            value = json.loads(row[0])
            value["source_snapshot_hash"] = sha256_jcs("forged")
            connection.execute(
                "UPDATE context_plans SET source_provenance_json=? WHERE plan_id=?",
                (json_text(value), target_plan),
            )
        else:
            connection.execute(
                "UPDATE fork_identity_mappings SET lineage_json='{}' WHERE entity_type='assembly'"
            )
    with pytest.raises(ValueError, match="source-mismatch"):
        saver.get_context_plan_registration(TARGET_SESSION_ID, plan_id=target_plan)


@pytest.mark.asyncio
async def test_draft_omission_stub_stays_target_local_without_body_or_contribution(
    protected_source,
):
    saver = protected_source[0]
    plan = ContextRequestPlan(
        session_id=SOURCE_SESSION_ID,
        plan_id="omitted-draft",
        refs=(
            ContextRef.request_only_ref(
                "unavailable-ref",
                session_id=SOURCE_SESSION_ID, thread_id="thread-1",
                plan_id="omitted-draft",
                source_revision="unavailable-revision",
                availability="unavailable",
            ),
        ),
        plan_creation_idempotency_key="omitted-create",
    )
    saver.create_context_plan(SOURCE_SESSION_ID, plan)
    await saver.afork(
        source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID, mode="full_rollout_copy"
    )
    with saver._storage._connect(TARGET_SESSION_ID, "", read_only=True) as connection:
        target_id = _target_plan_id(connection, plan.plan_id)
        copied = read_registration(connection, TARGET_SESSION_ID, target_id).draft
        assert copied.contributions == () and copied.selection == ()
        ref = copied.refs[0]
        assert (ref.session_id, ref.plan_id) == (TARGET_SESSION_ID, target_id)
        assert ref.ref_id != "unavailable-ref" and ref.availability == "unavailable"
        assert ref.content_hash is None and ref.content_length is None
        assert connection.execute(
            "SELECT count(*) FROM context_plan_contributions WHERE plan_id=?",
            (target_id,),
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_draft_only_contributions_and_aliases_are_remapped_without_source_lookup(
    protected_source,
):
    saver = protected_source[0]
    for plan_id, text in (("source-draft-a", "a"), ("source-draft-b", "b")):
        contribution = ContextContribution(
            contribution_id="draft-contribution",
            source_kind="instructions",
            source_revision=f"revision-{text}",
            body=text,
            content_hash=contribution_content_hash("prompt", text),
            metadata={"source_ref": "draft-alias"},
            source_ordinal=0,
        )
        ref = ContextRef.request_only_ref(
            "same-request-id",
            session_id=SOURCE_SESSION_ID, thread_id="thread-1",
            plan_id=plan_id,
            source_ref="draft-alias",
            source_revision=contribution.source_revision,
            content_hash_value=contribution.content_hash,
            content_length=contribution.content_length,
        )
        saver.create_context_plan(
            SOURCE_SESSION_ID,
            ContextRequestPlan(
                session_id=SOURCE_SESSION_ID,
                plan_id=plan_id,
                refs=(ref,),
                contributions=(contribution,),
                plan_creation_idempotency_key=f"create-{plan_id}",
            ),
        )
    await saver.afork(
        source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID, mode="full_rollout_copy"
    )
    with saver._storage._connect(TARGET_SESSION_ID, "", read_only=True) as connection:
        for source_id, text in (("source-draft-a", "a"), ("source-draft-b", "b")):
            target_id = _target_plan_id(connection, source_id)
            copied = read_registration(connection, TARGET_SESSION_ID, target_id).draft
            ref, contribution = copied.refs[0], copied.contributions[0]
            assert ref.plan_id == target_id and ref.session_id == TARGET_SESSION_ID
            assert ref.ref_id != "same-request-id" and ref.source_ref != "draft-alias"
            assert contribution.contribution_id != "draft-contribution"
            assert contribution.metadata["source_ref"] == ref.source_ref
            assert contribution.content_hash == contribution_content_hash(
                "prompt", text
            )
            assert contribution.body is None and contribution.assembly_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "property_name", ["plan_id", "ref_id", "source_ref", "redacted_stable_digest"]
)
async def test_tool_json_schema_property_names_are_not_identity_fields(
    protected_source, property_name
):
    saver = protected_source[0]
    tools = (
        {
            "name": "read_file",
            "parameters": {
                "type": "object",
                "properties": {property_name: {"type": "string"}},
            },
        },
    )
    tool = ToolSetRef.from_tool_snapshot(
        session_id=SOURCE_SESSION_ID,
        plan_id="schema-draft",
        snapshot_id="schema-tool",
        source_revision="schema-1",
        tools=tools,
    )
    saver.create_context_plan(
        SOURCE_SESSION_ID,
        ContextRequestPlan(
            session_id=SOURCE_SESSION_ID,
            plan_id="schema-draft",
            refs=(),
            tool_set_refs=(tool,),
            plan_creation_idempotency_key="schema-create",
        ),
    )
    await saver.afork(
        source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID, mode="full_rollout_copy"
    )
    with saver._storage._connect(TARGET_SESSION_ID, "", read_only=True) as connection:
        target_id = _target_plan_id(connection, "schema-draft")
        copied = read_registration(connection, TARGET_SESSION_ID, target_id).draft
        assert copied.tool_set_refs[0].tools == tools
        assert copied.tool_set_refs[0].content_hash == tool.content_hash
