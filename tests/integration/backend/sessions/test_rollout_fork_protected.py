"""真实 Saver→fork staging→AES/SQLite→重启读取的 protected copy 验收。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
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
from app.services.infrastructure.rollout_context.runtime.detail_keys import (
    ContextDetailKeyStore,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
    detail_record_from_mapping,
    detail_relative_path,
    protected_detail_relative_path,
    session_detail_digest,
)
from app.services.infrastructure.rollout_context.runtime.protected_detail import (
    ProtectedDetailBackend,
    ProtectedDetailError,
)


@pytest.fixture
def fork_workspace(integration_workspace_root_path, request):
    case = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    root = Path(integration_workspace_root_path) / "protected-fork-cases" / case
    root.mkdir(parents=True)
    return root


@pytest.fixture
def protected_key():
    return bytes(range(32))


@pytest.fixture
def protected_source(fork_workspace, session_bundle_factory, protected_key):
    sessions = fork_workspace / ".boxteam" / "sessions"
    for session in ("source", "target"):
        session_bundle_factory(sessions, session)
    with RolloutCheckpointSaver(sessions, protected_detail_key=protected_key) as saver:
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
        saver.put(
            build_checkpoint_config("source"),
            checkpoint,
            {"source": "integration"},
            {"messages": "1"},
        )
        body = [
            {"type": "text", "text": "独立 fixture secret，不得出现在任何普通 artifact"}
        ]
        session_key = ContextDetailKeyStore(get_session_path_resolver(sessions)).get(
            "source", create=True
        )
        digest = session_detail_digest(body, session_key=session_key)
        saver.register_context_contribution(
            "source",
            ContextContribution(
                contribution_id="source-contribution",
                source_kind="environment",
                source_revision="revision-1",
                content_length=len(canonical_json_bytes(body)),
                redacted_stable_digest=digest,
                protection="protected",
                metadata={"source_ref": "source-contribution"},
            ),
            request_content=body,
        )
        plan = saver.compose_committed_context_plan("source", plan_id="source-plan")
        plan = replace(
            plan, plan_creation_idempotency_key="protected-fork-source-create"
        )
        saver.create_context_plan("source", plan)
        sealed = saver.seal_context_plan(
            "source",
            plan,
            turn_id="source-turn",
            execution_id=saver.execution_for_turn("source", turn_id="source-turn"),
            provider_version="protected-fork-fixture",
            seal_idempotency_key="protected-fork-source-seal",
        )
        entry = next(
            entry for entry in sealed.selection if entry.ref.ref_type == "request_only"
        )
        record = detail_record_from_mapping(
            saver._storage.get_context_plan_detail(
                "source", detail_ref=entry.detail_ref
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
    protected_source, protected_key
):
    saver, sealed, source_record, body = protected_source
    source = saver._storage.root("source")
    original = _artifacts(source)
    source_blob = (
        source.parent / protected_detail_relative_path(source_record.detail_ref)
    ).read_bytes()
    await saver.afork(
        source_session_id="source", target_session_id="target", mode="full_rollout_copy"
    )
    assert _artifacts(source) == original

    target = saver._storage.root("target")
    assert (target / ".context-redaction-key").read_bytes() != (
        source / ".context-redaction-key"
    ).read_bytes()
    with saver._storage._connect("target", "", read_only=True) as connection:
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
            "session_id": "source",
            "assembly_id": sealed.assembly_id,
            "plan_id": sealed.plan_id,
            "plan_hash": sealed.plan_hash,
            "request_hash": sealed.request_hash,
        }
    snapshot = saver.get_context_assembly("target", assembly_id=assembly_id)
    entry = next(
        entry for entry in snapshot.selection if entry.ref.ref_type == "request_only"
    )
    target_record = detail_record_from_mapping(
        saver._storage.get_context_plan_detail("target", detail_ref=entry.detail_ref)
    )
    assert target_record.detail_id != source_record.detail_id
    assert target_record.assembly_id != source_record.assembly_id
    assert target_record.session_id == "target"
    assert snapshot.plan_id != sealed.plan_id
    assert entry.ref.plan_id == snapshot.plan_id
    assert entry.ref.ref_id != "source-contribution"
    assert entry.contribution_id != "source-contribution"
    assert target_record.redacted_stable_digest != source_record.redacted_stable_digest
    assert entry.ref.redacted_stable_digest == target_record.redacted_stable_digest
    assert target_record.expires_at == source_record.expires_at
    assert target_record.retention_class == source_record.retention_class
    assert target_record.visibility == source_record.visibility
    assert snapshot.plan_hash != sealed.plan_hash
    snapshot.validate_hashes()
    target_blob = (
        target.parent / protected_detail_relative_path(target_record.detail_ref)
    ).read_bytes()
    assert target_blob != source_blob
    assert not (target.parent / detail_relative_path(source_record.detail_ref)).exists()
    secret_bytes = body[0]["text"].encode()
    assert all(
        secret_bytes not in path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    )
    source.rename(source.with_name("source-preserved"))
    with RolloutCheckpointSaver(
        saver._storage.sessions_dir, protected_detail_key=protected_key
    ) as restarted:
        assert (
            restarted.read_context_plan_detail(
                "target", detail_ref=target_record.detail_ref, include_sensitive=True
            )["detail"]
            == body
        )
        restored = restarted.get_context_assembly("target", assembly_id=assembly_id)
        projected, losses = restarted.project_context_plan_with_diagnostics(
            "target", restored.as_sealed_plan()
        )
        assert not losses
        assert body[0]["text"] in json.dumps(
            [message.model_dump(mode="json") for message in projected],
            ensure_ascii=False,
        )
        with pytest.raises(PermissionError):
            restarted.read_context_plan_detail(
                "target", detail_ref=target_record.detail_ref
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "no_key",
        "wrong_key",
        "ciphertext",
        "source_digest",
        "missing_body",
        "missing_manifest",
    ],
)
async def test_protected_failure_never_installs_target(protected_source, failure):
    saver, _, record, body = protected_source
    source = saver._storage.root("source")
    if failure == "ciphertext":
        path = source.parent / protected_detail_relative_path(record.detail_ref)
        raw = path.read_bytes()
        path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    elif failure == "source_digest":
        (source / ".context-redaction-key").write_bytes(b"X" * 32)
    elif failure == "missing_body":
        (source.parent / protected_detail_relative_path(record.detail_ref)).unlink()
    elif failure == "missing_manifest":
        (source.parent / detail_relative_path(record.detail_ref)).unlink()
    original = _artifacts(source)
    if failure in {"no_key", "wrong_key"}:
        saver = RolloutCheckpointSaver(
            saver._storage.sessions_dir,
            protected_detail_key=None if failure == "no_key" else b"X" * 32,
        )
    with pytest.raises((RuntimeError, ValueError, DetailUnavailableError)) as error:
        await saver.afork(
            source_session_id="source",
            target_session_id="target",
            mode="full_rollout_copy",
        )
    assert body[0]["text"] not in str(error.value)
    assert not saver._storage.root("target").exists()
    assert not tuple(saver._storage.root("target").parent.glob(".fork-staging-*"))
    assert _artifacts(source) == original


@pytest.mark.asyncio
async def test_pinned_protected_copy_retains_lineage_not_shared_body(protected_source):
    saver, _, record, _ = protected_source
    source_root = saver._storage.root("source")
    original_key = (source_root / ".context-redaction-key").read_bytes()
    original_body = (
        source_root.parent / protected_detail_relative_path(record.detail_ref)
    ).read_bytes()
    await saver.afork(
        source_session_id="source",
        target_session_id="target",
        mode="full_rollout_copy",
        relationship="pinned",
    )
    with saver._storage._connect("source", "", read_only=True) as connection:
        rows = connection.execute(
            "SELECT owner_session_id, status FROM retention_refs WHERE reference_kind='fork'"
        ).fetchall()
        assert rows == [("target", "active")]
    assert (source_root / ".context-redaction-key").read_bytes() == original_key
    assert (
        source_root.parent / protected_detail_relative_path(record.detail_ref)
    ).read_bytes() == original_body


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["install", "encrypt"])
async def test_failure_before_install_has_no_publication(
    protected_source, monkeypatch, boundary
):
    from app.services.infrastructure.rollout_context.fork.full_copy import operation

    saver, _, _, body = protected_source
    original = _artifacts(saver._storage.root("source"))
    target = saver._storage.root("target")

    def fail_install(stage_root, target_root):
        assert target_root == target
        assert not target.exists()
        assert (stage_root / "index.sqlite").is_file()
        raise OSError("injected installation failure")

    def fail_encrypt(*args, **kwargs):
        assert not target.exists()
        raise ValueError(body[0]["text"])

    if boundary == "install":
        monkeypatch.setattr(operation, "install_staging", fail_install)
    else:
        monkeypatch.setattr(ProtectedDetailBackend, "encrypt", fail_encrypt)
    with pytest.raises((OSError, DetailUnavailableError)) as error:
        await saver.afork(
            source_session_id="source",
            target_session_id="target",
            mode="full_rollout_copy",
        )
    assert body[0]["text"] not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert not target.exists()
    assert not tuple(target.parent.glob(".fork-staging-*"))
    assert _artifacts(saver._storage.root("source")) == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("visibility", "private"),
        ("retention_class", "changed"),
        ("detail_kind", "changed"),
        ("source_revision", "changed"),
        ("length", 1),
        ("expires_at", "2000-01-01T00:00:00+00:00"),
        ("session_id", "target"),
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
    with pytest.raises((ValueError, DetailUnavailableError)):
        saver._detail_store.fork_detail_capability().prepare_detail(
            source_record=tampered,
            target_ref=DetailRef("target", "target-assembly", "target-detail"),
            target_session_key=b"T" * 32,
        )
    assert not saver._storage.root("target").exists()


def test_capability_random_nonce_and_typed_aad(protected_source, protected_key):
    saver, _, record, body = protected_source
    capability = saver._detail_store.fork_detail_capability()
    target_ref = DetailRef("target", "target-assembly", "target-detail")
    first = capability.prepare_detail(
        source_record=record, target_ref=target_ref, target_session_key=b"T" * 32
    )
    second = capability.prepare_detail(
        source_record=record, target_ref=target_ref, target_session_key=b"T" * 32
    )
    assert first.record == second.record
    assert first.manifest_bytes == second.manifest_bytes
    assert first.protected_bytes != second.protected_bytes
    assert body[0]["text"] not in repr(first)
    backend = ProtectedDetailBackend(protected_key)
    assert backend.decrypt(first.protected_bytes, record=first.record)["detail"] == body
    with pytest.raises(ProtectedDetailError):
        backend.decrypt(first.protected_bytes, record=record)
    assert first.record.redacted_stable_digest == session_detail_digest(
        body, session_key=b"T" * 32
    )
    assert not saver._storage.root("target").exists()


def test_capability_cannot_reuse_source_session_key(protected_source):
    saver, _, record, _ = protected_source
    source_key = ContextDetailKeyStore(
        get_session_path_resolver(saver._storage.sessions_dir)
    ).get("source", create=False)
    with pytest.raises(DetailUnavailableError, match="不得复用"):
        saver._detail_store.fork_detail_capability().prepare_detail(
            source_record=record,
            target_ref=DetailRef("target", "target-assembly", "target-detail"),
            target_session_key=source_key,
        )


@pytest.fixture
def header_detail_factory(protected_source):
    saver, _, _, body = protected_source

    def create(*, required: bool):
        plan = ContextRequestPlan(
            session_id="source",
            plan_id="header-plan",
            refs=(),
            plan_creation_idempotency_key="header-plan-create",
        )
        saver.create_context_plan("source", plan)
        snapshot = ContextPlanComposer().assembly(
            plan=plan,
            assembly_id="header-assembly",
            session_id="source",
            turn_id="source-turn",
            execution_id=saver.execution_for_turn("source", turn_id="source-turn"),
            provider_version="fixture-provider",
        )
        saver.seal_context_assembly(
            snapshot,
            detail={"body": body},
            sensitive_detail=True,
            required_detail=required,
            seal_idempotency_key="header-plan-seal",
            seal_input_hash=sha256_jcs({"required": required, "source": "header-plan"}),
        )
        with saver._storage._connect("source", "", read_only=True) as connection:
            key = connection.execute(
                "SELECT detail_ref FROM context_assemblies WHERE assembly_id='header-assembly'"
            ).fetchone()[0]
        record = detail_record_from_mapping(
            saver._storage.get_context_plan_detail(
                "source", detail_ref=detail_ref_from_key(key)
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
            saver._storage.root("source").parent
            / protected_detail_relative_path(record.detail_ref)
        ).unlink()
    await saver.afork(
        source_session_id="source", target_session_id="target", mode="full_rollout_copy"
    )
    with saver._storage._connect("target", "", read_only=True) as connection:
        assembly_id, detail_key = connection.execute(
            "SELECT a.assembly_id, a.detail_ref FROM context_assemblies a JOIN context_plan_details d ON a.detail_ref=d.detail_ref WHERE d.required=0"
        ).fetchone()
        assert connection.execute(
            "SELECT availability FROM context_plan_details WHERE detail_ref=?",
            (detail_key,),
        ).fetchone() == ("available" if available else "unavailable",)
    saver.get_context_assembly("target", assembly_id=assembly_id).validate_hashes()
    if not available:
        with pytest.raises(DetailUnavailableError):
            saver.read_context_plan_detail(
                "target",
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
    with saver._storage._connect("source", "") as connection:
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
        saver.get_context_assembly("source", assembly_id=record.assembly_id)
    with pytest.raises(RuntimeError, match="detail-unavailable"):
        await saver.afork(
            source_session_id="source",
            target_session_id="target",
            mode="full_rollout_copy",
        )
    assert not saver._storage.root("target").exists()
    assert not tuple(saver._storage.root("target").parent.glob(".fork-staging-*"))


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
    with saver._storage._connect("source", "") as connection:
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
        saver.get_context_assembly("source", assembly_id=record.assembly_id)
    with pytest.raises(RuntimeError, match="detail-unavailable"):
        await saver.afork(
            source_session_id="source",
            target_session_id="target",
            mode="full_rollout_copy",
        )
    assert not saver._storage.root("target").exists()
    assert not tuple(saver._storage.root("target").parent.glob(".fork-staging-*"))


def test_low_level_clone_never_publishes_protected_source(protected_source):
    saver, _, _, _ = protected_source
    with pytest.raises(DetailUnavailableError, match="staging"):
        saver._storage.clone_rollout(
            source_thread_id="source",
            target_thread_id="target",
            source_checkpoint_id=None,
            detail_capability=saver._detail_store.fork_detail_capability(),
        )
    assert not saver._storage.root("target").exists()


@pytest.mark.asyncio
async def test_fork_does_not_copy_unregistered_detail_orphans(protected_source):
    saver, _, record, _ = protected_source
    source_root = saver._storage.root("source")
    orphan = (
        source_root
        / "context-plan-details"
        / record.assembly_id
        / "unregistered-detail"
    )
    orphan.write_bytes(b"private orphan must not be copied")
    await saver.afork(
        source_session_id="source", target_session_id="target", mode="full_rollout_copy"
    )
    assert orphan.read_bytes() == b"private orphan must not be copied"
    assert not tuple(saver._storage.root("target").rglob("unregistered-detail"))
