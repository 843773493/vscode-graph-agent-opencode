"""真实 Saver→fork staging→单份 exact Detail/SQLite→重启读取的复制验收。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.request_plan import (
    ContextContribution,
    ContextRequestPlan,
    SourceBinding,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
    detail_record_from_mapping,
    detail_relative_path,
)
from tests.integration.backend.sessions.itemized_dispatch_helpers import EMPTY_TOOL_SET

SOURCE_SESSION_ID = "ses_019c205391bb7a4b81b6728df70f63e6"
TARGET_SESSION_ID = "ses_019b9187b87f78c78ef242bbd6043bfc"


@pytest.fixture
def fork_workspace(integration_workspace_root_path, request):
    case = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    root = (
        Path(integration_workspace_root_path)
        / "protected-fork-cases"
        / case
        / uuid4().hex
    )
    root.mkdir(parents=True)
    return root




@pytest.fixture
def protected_source(fork_workspace, session_bundle_factory):
    sessions = fork_workspace / ".boxteam" / "sessions"
    for session in (SOURCE_SESSION_ID, TARGET_SESSION_ID):
        session_bundle_factory(sessions, session)
    with RolloutCheckpointSaver(sessions) as saver:
        source_thread_id = saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "source-checkpoint"
        checkpoint["channel_values"] = {
            "messages": [
                HumanMessage(
                    content="source input",
                    id="source-user",
                    response_metadata={"turn_id": "source-turn"},
                ),
                AIMessage(content="source output", id="source-output"),
            ]
        }
        checkpoint["channel_versions"] = {"messages": "1"}
        accepted = saver.accept_turn(
            SOURCE_SESSION_ID,
            thread_id=source_thread_id,
            accepted_ingress_id="source-user",
            acceptance_idempotency_key="protected-source-acceptance",
            job_id="protected-source-job",
            admission_idempotency_key="protected-source-admission",
            payload="source input",
            turn_id="source-turn",
            root_item_id="item-source-user",
            initial_execution_id="protected-source-execution",
        )
        admission = saver.execution_admission(
            SOURCE_SESSION_ID,
            thread_id=source_thread_id,
            execution_id=str(accepted["initial_execution_id"]),
        )
        owner = MutationIntentOwner(
            session_id=SOURCE_SESSION_ID,
            thread_id=source_thread_id,
        )
        with (
            session_thread_owner_scope(
                owner,
                langgraph_thread_id=source_thread_id,
            ),
            execution_admission_scope(
                admission,
                langgraph_thread_id=source_thread_id,
            ),
        ):
            saver.put(
                build_checkpoint_config(source_thread_id),
                checkpoint,
                {"source": "integration"},
                {"messages": "1"},
            )
        body = [
            {"type": "text", "text": "独立 fixture exact sensitive body"}
        ]
        saver.register_context_contribution(
            SOURCE_SESSION_ID,
            source_thread_id,
            ContextContribution(
                contribution_id="source-contribution",
                content_hash=contribution_content_hash("prompt", body),
                source_kind="environment",
                source_revision="revision-1",
                content_length=len(canonical_json_bytes(body)),
                protection="protected",
                extensions={} , source_binding=SourceBinding(source_ref='source-contribution'),
            ),
            request_content=body,
        )
        plan = saver.compose_committed_context_plan(
            SOURCE_SESSION_ID,
            source_thread_id,
            plan_id="source-plan", tool_set=EMPTY_TOOL_SET,
        )
        plan = replace(
            plan, plan_creation_idempotency_key="protected-fork-source-create"
        )
        saver.create_context_plan(SOURCE_SESSION_ID, plan)
        sealed = saver.seal_context_plan(
            SOURCE_SESSION_ID,
            source_thread_id,
            plan,
            wire_profile=None, wire_params=None, turn_id="source-turn",
            execution_id=saver.execution_for_turn(
                SOURCE_SESSION_ID,
                turn_id="source-turn",
                thread_id=source_thread_id,
            ),
            provider_version="protected-fork-fixture",
            seal_idempotency_key="protected-fork-source-seal",
        )
        entry = next(
            entry for entry in sealed.selection if entry.ref.ref_type == "request_only"
        )
        record = detail_record_from_mapping(
            saver._storage.assemblies.get_context_plan_detail(
                SOURCE_SESSION_ID,
                source_thread_id,
                detail_ref=entry.detail_ref,
            )
        )
        yield saver, sealed, record, body


def _artifacts(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.asyncio
async def test_full_copy_protected_detached_restarts_without_source(
    protected_source
):
    saver, sealed, source_record, body = protected_source
    source_thread_id = saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)
    source = saver._storage.storage.root(SOURCE_SESSION_ID, thread_id=source_thread_id)
    original = _artifacts(source)
    source_blob = (
        source.parent / detail_relative_path(source_record.detail_ref)
    ).read_bytes()
    await saver.forks.afork(
        source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID, mode="full_rollout_copy"
    )
    assert _artifacts(source) == original

    target_thread_id = saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    target = saver._storage.storage.root(
        TARGET_SESSION_ID,
        thread_id=target_thread_id,
    )
    with saver._storage.storage._connect(
        target_thread_id,
        "",
        session_id=TARGET_SESSION_ID,
        read_only=True,
    ) as connection:
        assembly_id = connection.execute(
            "SELECT assembly_id FROM context_assemblies"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT status FROM fork_materializations"
        ).fetchall() == [("committed",)]
        assert connection.execute("SELECT COUNT(*) FROM fork_origins").fetchone() == (
            1,
        )
        lineage = json.loads(
            connection.execute(
                "SELECT lineage_json FROM fork_identity_mappings WHERE entity_type='assembly' AND source_local_id=?",
                (sealed.assembly_id,),
            ).fetchone()[0]
        )
        assert lineage["source_assembly"] == {
            "session_id": SOURCE_SESSION_ID,
            "assembly_id": sealed.assembly_id,
            "plan_id": sealed.plan_id,
            "plan_hash": sealed.plan_hash,
            "request_hash": sealed.request_hash,
        }
    snapshot = saver.get_context_assembly(
        TARGET_SESSION_ID,
        target_thread_id,
        assembly_id=assembly_id,
    )
    entry = next(
        entry for entry in snapshot.selection if entry.ref.ref_type == "request_only"
    )
    target_record = detail_record_from_mapping(
        saver._storage.assemblies.get_context_plan_detail(
            TARGET_SESSION_ID,
            target_thread_id,
            detail_ref=entry.detail_ref,
        )
    )
    assert target_record.detail_id != source_record.detail_id
    assert target_record.assembly_id != source_record.assembly_id
    assert target_record.session_id == TARGET_SESSION_ID
    assert snapshot.plan_id != sealed.plan_id
    assert entry.ref.plan_id == snapshot.plan_id
    assert entry.ref.ref_id != "source-contribution"
    assert entry.contribution_id != "source-contribution"
    original_entry = next(entry for entry in sealed.selection if entry.ref.ref_type == "request_only")
    assert entry.ref.content_hash == original_entry.ref.content_hash == contribution_content_hash("prompt", body)
    assert target_record.expires_at == source_record.expires_at
    assert target_record.retention_class == source_record.retention_class
    assert target_record.visibility == source_record.visibility
    assert snapshot.plan_hash != sealed.plan_hash
    snapshot.validate_hashes()
    target_blob = (
        target.parent / detail_relative_path(target_record.detail_ref)
    ).read_bytes()
    assert target_blob != source_blob
    assert not (target.parent / detail_relative_path(source_record.detail_ref)).exists()
    assert json.loads(target_blob)["detail"] == body
    assert json.loads(target_blob)["detail_content_hash"] == sha256_jcs(body)
    source.rename(source.with_name("source-preserved"))
    with RolloutCheckpointSaver(
        saver._storage.storage.sessions_dir
    ) as restarted:
        assert (
            restarted.read_context_plan_detail(
                TARGET_SESSION_ID,
                target_thread_id,
                detail_ref=target_record.detail_ref,
                include_sensitive=True,
            )["detail"]
            == body
        )
        restored = restarted.get_context_assembly(
            TARGET_SESSION_ID,
            target_thread_id,
            assembly_id=assembly_id,
        )
        projected, losses = restarted.project_context_plan_with_diagnostics(
            TARGET_SESSION_ID,
            target_thread_id,
            restored.as_sealed_plan(),
        )
        assert not losses
        assert body[0]["text"] in json.dumps(
            [message.model_dump(mode="json") for message in projected],
            ensure_ascii=False,
        )
        with pytest.raises(PermissionError):
            restarted.read_context_plan_detail(
                TARGET_SESSION_ID,
                target_thread_id,
                detail_ref=target_record.detail_ref,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "invalid_json",
        "body_tamper",
        "missing_body",
    ],
)
async def test_sensitive_failure_never_installs_target(protected_source, failure):
    saver, _, record, body = protected_source
    source_thread_id = saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)
    source = saver._storage.storage.root(SOURCE_SESSION_ID, thread_id=source_thread_id)
    path = source.parent / detail_relative_path(record.detail_ref)
    if failure == "invalid_json":
        path.write_bytes(b"{")
    elif failure == "body_tamper":
        payload = json.loads(path.read_bytes())
        payload["detail"] = [{"type": "text", "text": "different body"}]
        path.write_bytes(canonical_json_bytes(payload))
    else:
        path.unlink()
    original = _artifacts(source)
    with pytest.raises((RuntimeError, ValueError, DetailUnavailableError)) as error:
        await saver.forks.afork(
            source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID,
            mode="full_rollout_copy",
        )
    assert body[0]["text"] not in str(error.value)
    target_root = saver._storage.storage.root(
        TARGET_SESSION_ID, thread_id=saver._storage.storage.main_thread_id(TARGET_SESSION_ID),
    )
    assert not target_root.exists()
    assert not tuple(target_root.parent.glob(".fork-staging-*"))
    assert _artifacts(source) == original


@pytest.mark.asyncio
async def test_pinned_protected_copy_retains_lineage_not_shared_body(protected_source):
    saver, _, record, _ = protected_source
    source_thread_id = saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)
    source_root = saver._storage.storage.root(SOURCE_SESSION_ID, thread_id=source_thread_id)
    original_body = (
        source_root.parent / detail_relative_path(record.detail_ref)
    ).read_bytes()
    fork_id = uuid4().hex
    generation = saver.forks.recovery.begin_pinned_fork_retention_claim(
        fork_id=fork_id,
        source_session_id=SOURCE_SESSION_ID,
        target_session_id=TARGET_SESSION_ID,
    )
    await saver.forks.afork(
        source_session_id=SOURCE_SESSION_ID,
        target_session_id=TARGET_SESSION_ID,
        mode="full_rollout_copy",
        relationship="pinned",
        fork_id=fork_id,
    )
    with saver._storage.storage._connect(
        source_thread_id,
        "",
        session_id=SOURCE_SESSION_ID,
        read_only=True,
    ) as connection:
        rows = connection.execute(
            "SELECT owner_session_id, status FROM retention_refs WHERE reference_kind='fork'"
        ).fetchall()
        assert rows == [(TARGET_SESSION_ID, "active")]
    claim = saver._storage.forks._catalog_store().get_fork_retention_claim(fork_id)
    assert claim.state == "active"
    assert claim.source_lifecycle_generation == generation
    assert (
        source_root.parent / detail_relative_path(record.detail_ref)
    ).read_bytes() == original_body


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["install", "body_write"])
async def test_failure_before_install_has_no_publication(protected_source, monkeypatch, boundary):
    from app.services.infrastructure.rollout_context.fork.full_copy import (
        details,
        operation,
    )

    saver, _, _, body = protected_source
    source_thread_id = saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)
    target_thread_id = saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    original = _artifacts(saver._storage.storage.root(SOURCE_SESSION_ID, thread_id=source_thread_id))
    target = saver._storage.storage.root(TARGET_SESSION_ID, thread_id=target_thread_id)

    def fail_install(stage_root, target_root):
        assert target_root == target
        assert not target.exists()
        assert (stage_root / "index.sqlite").is_file()
        raise OSError("injected installation failure")

    def fail_body_write(path, raw):
        assert not target.exists()
        assert raw
        raise OSError("injected detail publication failure")

    if boundary == "install":
        monkeypatch.setattr(operation, "install_staging", fail_install)
    else:
        monkeypatch.setattr(details, "write_private", fail_body_write)
    with pytest.raises((OSError, DetailUnavailableError)) as error:
        await saver.forks.afork(
            source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID,
            mode="full_rollout_copy",
        )
    assert body[0]["text"] not in str(error.value)
    assert not target.exists()
    assert not tuple(target.parent.glob(".fork-staging-*"))
    assert _artifacts(saver._storage.storage.root(SOURCE_SESSION_ID, thread_id=source_thread_id)) == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("visibility", "private"),
        ("retention_class", "changed"),
        ("detail_kind", "changed"),
        ("source_revision", "changed"),
        ("length", 1),
        ("expires_at", "2000-01-01T00:00:00+00:00"),
        ("session_id", TARGET_SESSION_ID),
        ("assembly_id", "other-assembly"),
        ("detail_id", "other-detail"),
        ("checkpoint_ns", "other"),
    ],
)
def test_capability_rejects_tampered_source_manifest(protected_source, field, value):
    saver, _, source_record, _ = protected_source
    changes = {field: value}
    if field in {"session_id", "assembly_id", "detail_id"}:
        new_ref = replace(source_record.detail_ref, **changes)
        changes["relative_path"] = detail_relative_path(new_ref).as_posix()
    tampered = replace(source_record, **changes)
    target_thread_id = saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    with pytest.raises((ValueError, DetailUnavailableError)):
        saver._detail_store.fork_detail_capability().prepare_detail(
            source_record=tampered,
            target_ref=DetailRef(
                TARGET_SESSION_ID,
                target_thread_id,
                "target-assembly",
                "target-detail",
            ),
        )
    assert not saver._storage.storage.root(
        TARGET_SESSION_ID,
        thread_id=saver._storage.storage.main_thread_id(TARGET_SESSION_ID),
    ).exists()


def test_capability_binds_exact_body_and_target_local_identity(protected_source):
    saver, _, record, body = protected_source
    capability = saver._detail_store.fork_detail_capability()
    target_ref = DetailRef(
        TARGET_SESSION_ID, saver._storage.storage.main_thread_id(TARGET_SESSION_ID),
        "target-assembly", "target-detail",
    )
    first = capability.prepare_detail(source_record=record, target_ref=target_ref)
    second = capability.prepare_detail(source_record=record, target_ref=target_ref)
    assert first.record == second.record
    assert first.manifest_bytes == second.manifest_bytes
    assert body[0]["text"] not in repr(first)
    payload = json.loads(first.manifest_bytes)
    assert payload["detail"] == body
    assert payload["detail_content_hash"] == sha256_jcs(body)
    assert payload["detail_ref"] == target_ref.to_dict()
    assert first.record.content_hash == sha256_jcs(payload)
    assert first.record.length == record.length == len(canonical_json_bytes(body))
    assert first.record.source_revision == record.source_revision
    assert first.record.expires_at == record.expires_at
    assert not saver._storage.storage.root(TARGET_SESSION_ID, thread_id=target_ref.thread_id).exists()


def test_capability_cannot_reuse_source_owner(protected_source):
    saver, _, record, _ = protected_source
    with pytest.raises(ValueError, match="owner 必须不同"):
        saver._detail_store.fork_detail_capability().prepare_detail(
            source_record=record,
            target_ref=DetailRef(record.session_id, record.thread_id, "target-assembly", "target-detail"),
        )


@pytest.fixture
def header_detail_factory(protected_source):
    saver, _, _, body = protected_source

    def create(*, required: bool):
        plan = ContextRequestPlan(
            session_id=SOURCE_SESSION_ID,
            thread_id=saver._storage.storage.main_thread_id(SOURCE_SESSION_ID),
            plan_id="header-plan",
            refs=(),
            plan_creation_idempotency_key="header-plan-create",
        )
        source_thread_id = saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)
        saver.create_context_plan(SOURCE_SESSION_ID, plan)
        snapshot = ContextPlanComposer().assembly(
            plan=plan,
            assembly_id="header-assembly",
            session_id=SOURCE_SESSION_ID,
            thread_id=source_thread_id,
            turn_id="source-turn",
            execution_id=saver.execution_for_turn(
                SOURCE_SESSION_ID,
                turn_id="source-turn",
                thread_id=source_thread_id,
            ),
            provider_version="fixture-provider",
        )
        saver.seal_context_assembly(
            SOURCE_SESSION_ID,
            source_thread_id,
            snapshot,
            detail={"body": body},
            sensitive_detail=True,
            required_detail=required,
            seal_idempotency_key="header-plan-seal",
            seal_input_hash=sha256_jcs({"required": required, "source": "header-plan"}),
        )
        with saver._storage.storage._connect(
            source_thread_id,
            "",
            session_id=SOURCE_SESSION_ID,
            read_only=True,
        ) as connection:
            key = connection.execute(
                "SELECT detail_ref FROM context_assemblies WHERE assembly_id='header-assembly'"
            ).fetchone()[0]
        record = detail_record_from_mapping(
            saver._storage.assemblies.get_context_plan_detail(
                SOURCE_SESSION_ID,
                source_thread_id,
                detail_ref=detail_ref_from_key(key),
            )
        )
        return saver, record

    return create


@pytest.fixture
def optional_header(header_detail_factory):
    return header_detail_factory(required=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_optional_header_preserves_explicit_availability(
    optional_header, available
):
    saver, record = optional_header
    if not available:
        (
            saver._storage.storage.root(
                SOURCE_SESSION_ID,
                thread_id=saver._storage.storage.main_thread_id(SOURCE_SESSION_ID),
            ).parent
            / detail_relative_path(record.detail_ref)
        ).unlink()
    await saver.forks.afork(
        source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID, mode="full_rollout_copy"
    )
    target_thread_id = saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    with saver._storage.storage._connect(
        target_thread_id,
        "",
        session_id=TARGET_SESSION_ID,
        read_only=True,
    ) as connection:
        assembly_id, detail_key = connection.execute(
            "SELECT a.assembly_id, a.detail_ref FROM context_assemblies a JOIN context_plan_details d ON a.detail_ref=d.detail_ref WHERE d.required=0"
        ).fetchone()
        assert connection.execute(
            "SELECT availability FROM context_plan_details WHERE detail_ref=?",
            (detail_key,),
        ).fetchone() == ("available" if available else "unavailable",)
    target_thread_id = saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    saver.get_context_assembly(
        TARGET_SESSION_ID,
        target_thread_id,
        assembly_id=assembly_id,
    ).validate_hashes()
    if not available:
        with pytest.raises(DetailUnavailableError):
            saver.read_context_plan_detail(
                TARGET_SESSION_ID,
                target_thread_id,
                detail_ref=detail_ref_from_key(detail_key),
                include_sensitive=True,
            )


@pytest.mark.asyncio
async def test_required_header_unavailable_rejects_restore_and_fork(
    header_detail_factory,
):
    saver, record = header_detail_factory(required=True)
    assert record.required is True
    assert record.detail_kind == "assembly_snapshot"
    source_thread_id = saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)
    with saver._storage.storage._connect(
        source_thread_id,
        "",
        session_id=SOURCE_SESSION_ID,
    ) as connection:
        assert (
            connection.execute(
                "UPDATE context_plan_details SET status='unavailable', availability='unavailable' "
                "WHERE detail_ref=? AND required=1",
                (detail_ref_key(record.detail_ref),),
            ).rowcount
            == 1
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="detail-unavailable"):
        saver.get_context_assembly(
            SOURCE_SESSION_ID,
            source_thread_id,
            assembly_id=record.assembly_id,
        )
    with pytest.raises(RuntimeError, match="detail-unavailable"):
        await saver.forks.afork(
            source_session_id=SOURCE_SESSION_ID,
            target_session_id=TARGET_SESSION_ID,
            mode="full_rollout_copy",
        )
    target_root = saver._storage.storage.root(
        TARGET_SESSION_ID,
        thread_id=saver._storage.storage.main_thread_id(TARGET_SESSION_ID),
    )
    assert not target_root.exists()
    assert not tuple(target_root.parent.glob(".fork-staging-*"))


@pytest.mark.asyncio
async def test_included_source_unavailable_rejects_corrupted_optional_flag(
    protected_source,
):
    saver, snapshot, record, _ = protected_source
    entry = next(
        entry for entry in snapshot.selection if entry.detail_ref == record.detail_ref
    )
    assert entry.included is True
    assert record.required is True
    assert record.detail_kind == "request_source"
    source_thread_id = saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)
    with saver._storage.storage._connect(
        source_thread_id,
        "",
        session_id=SOURCE_SESSION_ID,
    ) as connection:
        assert (
            connection.execute(
                "UPDATE context_plan_details SET required=0, status='unavailable', "
                "availability='unavailable' WHERE detail_ref=? AND required=1",
                (detail_ref_key(record.detail_ref),),
            ).rowcount
            == 1
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="detail-unavailable"):
        saver.get_context_assembly(
            SOURCE_SESSION_ID,
            source_thread_id,
            assembly_id=record.assembly_id,
        )
    with pytest.raises(RuntimeError, match="detail-unavailable"):
        await saver.forks.afork(
            source_session_id=SOURCE_SESSION_ID,
            target_session_id=TARGET_SESSION_ID,
            mode="full_rollout_copy",
        )
    target_root = saver._storage.storage.root(
        TARGET_SESSION_ID,
        thread_id=saver._storage.storage.main_thread_id(TARGET_SESSION_ID),
    )
    assert not target_root.exists()
    assert not tuple(target_root.parent.glob(".fork-staging-*"))


def test_low_level_clone_never_publishes_protected_source(protected_source):
    saver, _, _, _ = protected_source
    with pytest.raises(DetailUnavailableError, match="staging"):
        saver._storage.forks.clone_rollout(
            source_session_id=SOURCE_SESSION_ID,
            source_thread_id=saver._storage.storage.main_thread_id(SOURCE_SESSION_ID),
            target_session_id=TARGET_SESSION_ID,
            target_thread_id=saver._storage.storage.main_thread_id(TARGET_SESSION_ID),
            source_checkpoint_id=None,
            detail_capability=saver._detail_store.fork_detail_capability(),
        )
    assert not saver._storage.storage.root(
        TARGET_SESSION_ID,
        thread_id=saver._storage.storage.main_thread_id(TARGET_SESSION_ID),
    ).exists()


@pytest.mark.asyncio
async def test_fork_does_not_copy_unregistered_detail_orphans(protected_source):
    saver, _, record, _ = protected_source
    source_root = saver._storage.storage.root(
        SOURCE_SESSION_ID,
        thread_id=saver._storage.storage.main_thread_id(SOURCE_SESSION_ID),
    )
    orphan = (
        source_root
        / "context-plan-details"
        / record.assembly_id
        / "unregistered-detail"
    )
    orphan.write_bytes(b"private orphan must not be copied")
    await saver.forks.afork(
        source_session_id=SOURCE_SESSION_ID, target_session_id=TARGET_SESSION_ID, mode="full_rollout_copy"
    )
    assert orphan.read_bytes() == b"private orphan must not be copied"
    assert not tuple(
        saver._storage.storage.root(
            TARGET_SESSION_ID,
            thread_id=saver._storage.storage.main_thread_id(TARGET_SESSION_ID),
        ).rglob("unregistered-detail")
    )
