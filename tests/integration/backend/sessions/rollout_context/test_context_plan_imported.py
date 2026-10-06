"""真实 accept/seal/fork 后的 immutable import 与防篡改合同。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace

import pytest

from app.core.identifier import create_uuid_hex
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.request_plan import (
    ContextContribution,
    ContextRequestPlan,
    SourceBinding,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.assembly.plans.imported import (
    imported_registration_hash,
    read_imported_registration,
    write_imported_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.imported_sources import (
    parse_source_manifest,
)
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    sealed_hash,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    read_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.sealing import (
    preflight_seal,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.migration.artifacts import (
    artifact_manifest,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as json_text,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    _insert_snapshot,
)
from tests.integration.backend.sessions.rollout_test_workspace import (
    prepare_rollout_workspace,
)


@dataclass
class ForkImportCase:
    saver: RolloutCheckpointSaver
    session_id: str
    connection: sqlite3.Connection
    snapshot: ContextAssemblySnapshot
    draft: ContextRequestPlan
    provenance: dict[str, object]
    source_manifest: dict[str, object]


@pytest.fixture
def fork_plan_kind(request):
    return getattr(request, "param", "included")


@pytest.fixture
def fork_case(request, session_bundle_factory, fork_plan_kind):
    sessions = prepare_rollout_workspace(request) / ".boxteam" / "sessions"
    source, target = (f"ses_{create_uuid_hex()}" for _ in range(2))
    for session in (source, target):
        session_bundle_factory(sessions, session)
    body = [{"type": "text", "text": "草稿正文不能复制进 SQLite"}]
    with RolloutCheckpointSaver(sessions) as saver:
        storage = saver._storage
        source_thread, target_thread = (storage.main_thread_id(session) for session in (source, target))
        accepted = saver.accept_turn(
            source, thread_id=source_thread, accepted_ingress_id="source-ingress",
            acceptance_idempotency_key="source-accept", job_id="source-job",
            admission_idempotency_key="source-admission", payload="真实 fork 来源",
        )
        contribution = ContextContribution(
            contribution_id="source-contribution", source_kind="workspace_instructions",
            source_revision="source-revision", body=body,
            content_hash=contribution_content_hash("prompt", body),
            source_binding=SourceBinding(source_ref="source-contribution"),
        )
        if fork_plan_kind != "unbound_omitted":
            saver.register_context_contribution(source, source_thread, contribution, request_content=body)
        tools = ({"name": "read_file", "parameters": {"type": "object"}},)
        plan = saver.compose_committed_context_plan(
            source, source_thread, plan_id="source-plan",
            tool_set=ToolSetSnapshot(selection_revision=sha256_jcs(tools), tools=tools),
        )
        plan = replace(
            plan, refs=tuple(ref for ref in plan.refs if ref.ref_type == "request_only"),
            plan_creation_idempotency_key="source-plan-create",
        )
        if fork_plan_kind == "known_omitted":
            plan = replace(
                plan, refs=tuple(replace(ref, availability="unavailable") for ref in plan.refs),
                tool_set_refs=(),
            )
        elif fork_plan_kind == "unbound_omitted":
            plan = replace(
                plan, refs=(ContextRef.request_only_ref(
                    "omitted-ref", session_id=source, thread_id=source_thread,
                    plan_id=plan.plan_id, source_revision="known-revision",
                    availability="unavailable",
                ),), contributions=(), tool_set_refs=(),
            )
        saver.create_context_plan(source, plan)
        if fork_plan_kind == "known_omitted":
            # 已知 omitted 映射来自真实 draft；通过当前低层 seal owner 封存。
            original = ContextPlanComposer().assembly(
                plan=plan, session_id=source, thread_id=source_thread,
                assembly_id="source-omitted-assembly", turn_id=accepted["turn_id"],
                execution_id=accepted["initial_execution_id"], provider_version="import-fixture",
            )
            original = replace(
                original, selection=(replace(
                    original.selection[0], contribution_id=contribution.contribution_id,
                ),),
            )
            sealed_plan = original.as_sealed_plan()
            original = replace(
                original, plan_hash=sealed_plan.plan_hash(),
                request_hash=context_request_hash(
                    sealed_plan, original.provider_version, projector_id=original.projector_id,
                    projector_version=original.projector_version, target_format=original.target_format,
                    wire_request=original.request_hash_preimage,
                ),
            )
            saver.seal_context_assembly(
                source, source_thread, original, seal_idempotency_key="source-plan-seal",
                seal_input_hash=sha256_jcs({"fixture": "known-omitted-source"}),
            )
            original = saver.get_context_assembly(source, source_thread, assembly_id=original.assembly_id)
        else:
            original = saver.seal_context_plan(
                source, source_thread, plan, wire_profile=None, wire_params=None, turn_id=accepted["turn_id"],
                execution_id=accepted["initial_execution_id"], provider_version="import-fixture",
                seal_idempotency_key="source-plan-seal",
            )
        source_view = storage.clone_rollout(
            source_session_id=source, source_thread_id=source_thread,
            target_session_id=target, target_thread_id=target_thread, source_checkpoint_id=None,
        )
        fields = {
            "source_session_id": source, "target_session_id": target,
            "source_checkpoint_id": None, "source_view_id": source_view,
            "fork_mode": "full_rollout_copy", "relationship": "detached", "checkpoint_ns": "",
            "source_thread_id": source_thread, "target_thread_id": target_thread,
            "source_snapshot_sha256": hashlib.sha256(
                canonical_json_bytes(artifact_manifest(storage.root(source)))
            ).hexdigest(),
        }
        materialization, fork_id = storage.begin_fork_materialization(**fields)
        storage.commit_fork_materialization(materialization, **fields)
        with storage._connect(target_thread, "", read_only=True, session_id=target) as owner_connection:
            database_path = owner_connection.execute("PRAGMA database_list").fetchone()[2]
            target_assembly = owner_connection.execute(
                "SELECT target_local_id FROM fork_identity_mappings WHERE fork_id=? "
                "AND entity_type='assembly' AND source_local_id=?",
                (fork_id, original.assembly_id),
            ).fetchone()[0]
        current = saver.get_context_assembly(target, target_thread, assembly_id=target_assembly)
        connection = sqlite3.connect(database_path)
        connection.execute("PRAGMA foreign_keys=ON")
        registration = read_imported_registration(connection, target, target_thread, current.plan_id)
        provenance = registration.source_provenance
        with storage._connect(source_thread, "", read_only=True, session_id=source) as source_connection:
            source_version = source_connection.execute(
                "SELECT schema_version FROM database_meta WHERE singleton_id=1"
            ).fetchone()[0]
            journal_version = source_connection.execute(
                "SELECT to_version FROM schema_migrations WHERE status='completed' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        assert provenance["source_schema_version"] == source_version == journal_version == storage_version.ROLLOUT_SCHEMA_VERSION
        assert provenance["source_snapshot_hash"] == sha256_jcs(original.to_dict())
        assert provenance["audit_id"] == fork_id
        sources = registration.source_manifest
        parsed = parse_source_manifest(
            sources, session_id=target, thread_id=target_thread, plan_id=current.plan_id,
        )
        target_draft = ContextRequestPlan(
            session_id=target, thread_id=target_thread, plan_id=current.plan_id,
            refs=parsed.refs,
            contributions=tuple(replace(item, body=body) for item in parsed.contributions),
            tool_set_refs=tuple(replace(ref, assembly_id=None) for ref in current.tool_set_refs),
            plan_creation_idempotency_key="port-fixture-create",
        )
        # 隔离 port 的重写区；来源、snapshot、manifest 和 audit 均取自上面的真实 fork。
        # 不创建来源映射，不把损坏目标交给正常 storage 恢复或做回填。
        with connection:
            for table in (
                "context_plan_refs", "context_plan_contributions", "tool_set_snapshots",
                "context_plan_seal_failures", "context_plans", "context_assemblies",
            ):
                connection.execute(f"DELETE FROM {table}")
        try:
            yield ForkImportCase(saver, target, connection, current, target_draft, provenance, sources)
        finally:
            connection.close()


@pytest.fixture
def registry_db(fork_case):
    value = fork_case.snapshot
    return fork_case.saver, fork_case.session_id, fork_case.connection, {
        "turn_id": value.turn_id, "initial_execution_id": value.execution_id,
    }


@pytest.fixture
def draft(fork_case):
    return fork_case.draft


@pytest.fixture
def snapshot(fork_case):
    return fork_case.snapshot


@pytest.fixture
def source_manifest(fork_case):
    return fork_case.source_manifest


@pytest.fixture
def provenance(fork_case):
    return fork_case.provenance


@pytest.fixture
def import_snapshot(registry_db, snapshot, provenance, source_manifest):
    _saver, _session, connection, _accepted = registry_db

    def write(
        *, value=snapshot, source=provenance, origin="fork_import", detail=None,
        sources=source_manifest,
    ):
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _insert_snapshot(connection, value)
            if detail is not None:
                connection.execute(
                    "UPDATE context_assemblies SET detail_ref = ?", (detail,)
                )
            write_imported_registration(
                connection,
                value,
                detail_key=detail,
                origin=origin,
                source_provenance=source,
                source_manifest=sources,
                seal_idempotency_key="import-seal",
            )

    return write


def test_import_roundtrip_is_sealed_without_fabricated_draft(
    registry_db, snapshot, provenance, import_snapshot, source_manifest
):
    _saver, session, connection, _accepted = registry_db
    source = dict(provenance)
    origin = "fork_import"
    import_snapshot(source=source, origin=origin)
    registered = read_registration(
        connection, session, snapshot.thread_id, snapshot.plan_id
    )
    assert registered == read_imported_registration(
        connection, session, snapshot.thread_id, snapshot.plan_id
    )
    assert registered.draft is None and registered.creation_hash is None
    assert registered.registration_origin == origin
    assert registered.source_provenance == source
    assert registered.source_manifest == source_manifest
    assert registered.plan_state == "sealed" and registered.revision == 0
    assert registered.assembly_id == snapshot.assembly_id
    assert registered.seal_hash == imported_registration_hash(
        snapshot, detail_key=None, origin=origin, source_provenance=source,
        source_manifest=source_manifest,
    )
    assert (
        connection.execute(
            "SELECT plan_creation_idempotency_key, creation_json, creation_hash, draft_json, draft_hash "
            "FROM context_plans"
        ).fetchone()
        == (None,) * 5
    )
    assert connection.execute(
        "SELECT assembly_id FROM tool_set_snapshots"
    ).fetchall() == [(snapshot.assembly_id,)]
    manifests = connection.execute(
        "SELECT manifest_json FROM context_plan_contributions"
    ).fetchall()
    assert len(manifests) == 1
    assert json.loads(manifests[0][0])["body"] is None
    assert "草稿正文不能复制进 SQLite" not in manifests[0][0]
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    with pytest.raises(ValueError, match="不可再次 seal"):
        preflight_seal(
            connection, snapshot, seal_idempotency_key="import-seal", detail_key=None,
            seal_input_hash=sha256_jcs({"caller": "must-reject-imported-plan"}),
        )


def test_import_requires_transaction_and_existing_same_assembly(
    registry_db, snapshot, provenance, source_manifest
):
    _saver, _session, connection, _accepted = registry_db
    arguments = {
        "detail_key": None,
        "origin": "fork_import",
        "source_provenance": provenance,
        "source_manifest": source_manifest,
        "seal_idempotency_key": "import-seal",
    }
    with pytest.raises(RuntimeError, match="显式事务"):
        write_imported_registration(connection, snapshot, **arguments)
    with pytest.raises(ValueError, match="恰好绑定一个"), connection:
        connection.execute("BEGIN IMMEDIATE")
        write_imported_registration(connection, snapshot, **arguments)
    assert connection.execute("SELECT count(*) FROM context_plans").fetchone() == (0,)


@pytest.mark.parametrize(
    "field",
    [
        "source_session_id",
        "source_plan_id",
        "source_assembly_id",
        "source_snapshot_hash",
        "source_schema_version",
        "audit_id",
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "empty", "wrong_type"])
def test_import_provenance_rejects_missing_or_invalid_fields(
    registry_db, provenance, import_snapshot, field, mutation
):
    source = dict(provenance)
    if mutation == "missing":
        source.pop(field)
    else:
        source[field] = "" if mutation == "empty" else True
    with pytest.raises(ValueError, match="provenance"):
        import_snapshot(source=source)
    assert registry_db[2].execute("SELECT count(*) FROM context_plans").fetchone() == (
        0,
    )
    assert registry_db[2].execute(
        "SELECT count(*) FROM context_assemblies"
    ).fetchone() == (0,)


@pytest.mark.parametrize("mutation", ["extra", "bad_hash", "future", "old_version", "unknown_origin"])
def test_import_provenance_is_closed(provenance, import_snapshot, mutation):
    source = dict(provenance)
    origin = "fork_import"
    if mutation == "extra":
        source["source_body"] = "must not persist"
    elif mutation == "bad_hash":
        source["source_snapshot_hash"] = "sha256:jcs:v1:" + "A" * 64
    elif mutation == "future":
        source["source_schema_version"] = storage_version.ROLLOUT_SCHEMA_VERSION + 1
    elif mutation == "old_version":
        source["source_schema_version"] = 4
    else:
        origin = "unknown"
    with pytest.raises(ValueError, match="source-mismatch"):
        import_snapshot(source=source, origin=origin)


@pytest.mark.parametrize(
    "table", ["context_plan_refs", "context_plan_contributions", "tool_set_snapshots"]
)
def test_import_read_rejects_missing_rows_without_backfill(
    registry_db, snapshot, import_snapshot, table
):
    import_snapshot()
    _saver, session, connection, _accepted = registry_db
    with connection:
        connection.execute(f"DELETE FROM {table}")
    with pytest.raises(ValueError, match="source-mismatch"):
        read_registration(connection, session, snapshot.thread_id, snapshot.plan_id)
    assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


@pytest.mark.parametrize(
    "mutation",
    [
        "body",
        "ref_owner",
        "tool_policy",
        "seal_hash",
        "provenance",
        "snapshot_hash",
        "assembly_column",
    ],
)
def test_import_recovery_rejects_tampering(
    registry_db, snapshot, import_snapshot, mutation
):
    import_snapshot()
    _saver, session, connection, _accepted = registry_db
    with connection:
        if mutation == "body":
            value = json.loads(
                connection.execute(
                    "SELECT manifest_json FROM context_plan_contributions"
                ).fetchone()[0]
            )
            value["body"] = "injected inline body"
            connection.execute(
                "UPDATE context_plan_contributions SET manifest_json=?",
                (json_text(value),),
            )
        elif mutation == "ref_owner":
            value = snapshot.refs[0].to_dict()
            value["session_id"] = "wrong-session"
            connection.execute(
                "UPDATE context_plan_refs SET ref_json=?", (json_text(value),)
            )
        elif mutation == "tool_policy":
            connection.execute(
                "UPDATE tool_set_snapshots SET tool_policy_json=?",
                (json_text({"headers": {"Authorization": "do-not-leak"}}),),
            )
        elif mutation == "seal_hash":
            connection.execute("UPDATE context_plans SET seal_hash='wrong'")
        elif mutation == "provenance":
            connection.execute("UPDATE context_plans SET source_provenance_json='{}'")
        elif mutation == "assembly_column":
            connection.execute("UPDATE context_assemblies SET plan_hash='wrong'")
        else:
            value = snapshot.to_dict()
            value["request_hash"] = "wrong"
            connection.execute(
                "UPDATE context_assemblies SET snapshot_json=?", (json_text(value),)
            )
    with pytest.raises(ValueError, match="source-mismatch") as caught:
        read_registration(connection, session, snapshot.thread_id, snapshot.plan_id)
    assert "do-not-leak" not in str(caught.value)


def test_import_retry_is_exact_and_does_not_create_second_plan(
    registry_db, snapshot, provenance, import_snapshot, source_manifest
):
    import_snapshot()
    connection = registry_db[2]
    before = tuple(connection.iterdump())
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        write_imported_registration(
            connection,
            snapshot,
            detail_key=None,
            origin="fork_import",
            source_provenance=provenance,
            source_manifest=source_manifest,
            seal_idempotency_key="import-seal",
        )
    assert tuple(connection.iterdump()) == before
    with pytest.raises(ValueError, match="idempotency-conflict"), connection:
        connection.execute("BEGIN IMMEDIATE")
        write_imported_registration(
            connection,
            snapshot,
            detail_key=None,
            origin="fork_import",
            source_provenance=provenance,
            source_manifest=source_manifest,
            seal_idempotency_key="other-seal",
        )
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("owner", ["session", "thread", "assembly"])
def test_import_rejects_cross_owner_header_detail(
    registry_db, snapshot, import_snapshot, owner
):
    detail = DetailRef(
        "other" if owner == "session" else snapshot.session_id,
        "foreign-thread" if owner == "thread" else snapshot.thread_id,
        "other" if owner == "assembly" else snapshot.assembly_id,
        "header",
    )
    with pytest.raises(ValueError, match="source-mismatch"):
        import_snapshot(detail=detail_ref_key(detail))
    assert registry_db[2].execute("SELECT count(*) FROM context_plans").fetchone() == (
        0,
    )


def test_import_one_plan_cannot_name_multiple_assemblies(
    registry_db, snapshot, provenance, source_manifest
):
    connection = registry_db[2]
    other = replace(
        snapshot,
        assembly_id="other",
        contributions=(),
        refs=(),
        selection=(),
        tool_set_refs=(),
    )
    with pytest.raises(ValueError, match="恰好绑定一个"), connection:
        connection.execute("BEGIN IMMEDIATE")
        _insert_snapshot(connection, snapshot)
        _insert_snapshot(connection, other)
        write_imported_registration(
            connection,
            snapshot,
            detail_key=None,
            origin="fork_import",
            source_provenance=provenance,
            source_manifest=source_manifest,
            seal_idempotency_key="import-seal",
        )
    assert connection.execute("SELECT count(*) FROM context_plans").fetchone() == (0,)


@pytest.mark.parametrize(
    "field",
    [
        "source_session_id",
        "source_plan_id",
        "source_assembly_id",
        "source_snapshot_hash",
        "audit_id",
        "origin",
    ],
)
def test_import_hash_binds_each_source_field(
    registry_db, snapshot, provenance, import_snapshot, field
):
    import_snapshot()
    _saver, session, connection, _accepted = registry_db
    source = dict(provenance)
    origin = "fork_import"
    if field == "origin":
        origin = "runtime"
        connection.execute("PRAGMA ignore_check_constraints=ON")
    else:
        source[field] = (
            sha256_jcs("other") if field == "source_snapshot_hash" else "other"
        )
    with connection:
        connection.execute(
            "UPDATE context_plans SET registration_origin=?, source_provenance_json=?",
            (origin, json_text(source)),
        )
    connection.execute("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(ValueError, match="source-mismatch"):
        read_registration(connection, session, snapshot.thread_id, snapshot.plan_id)


def test_import_hash_namespace_is_independent(snapshot, provenance, source_manifest):
    assert imported_registration_hash(
        snapshot, detail_key=None, origin="fork_import", source_provenance=provenance,
        source_manifest=source_manifest,
    ) == sha256_jcs(
        {
            "schema": "context-plan-import:v2",
            "registration_origin": "fork_import",
            "source_provenance": provenance,
            "source_manifest": source_manifest,
            "sealed_hash": sealed_hash(snapshot, None),
        }
    )


@pytest.mark.parametrize("protection", ["public", "protected"])
def test_import_snapshot_json_inline_body_is_rejected_before_normalization(
    registry_db, snapshot, import_snapshot, protection
):
    import_snapshot()
    _saver, session, connection, _accepted = registry_db
    value = snapshot.to_dict()
    value["contributions"][0].update(
        body="secret not allowed even if ignored by hashes", protection=protection
    )
    with connection:
        connection.execute(
            "UPDATE context_assemblies SET snapshot_json=?", (json_text(value),)
        )
    with pytest.raises(ValueError, match="snapshot schema/hash/privacy") as caught:
        read_registration(connection, session, snapshot.thread_id, snapshot.plan_id)
    assert "secret not allowed" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert connection.execute(
        "SELECT snapshot_json FROM context_assemblies"
    ).fetchone()[0] == json_text(value)


def test_import_writer_rejects_inline_body(
    registry_db, snapshot, draft, import_snapshot
):
    value = replace(
        snapshot,
        contributions=(
            replace(snapshot.contributions[0], body=draft.contributions[0].body),
        ),
    )
    with pytest.raises(ValueError, match="inline contribution body"):
        import_snapshot(value=value)
    assert registry_db[2].execute("SELECT count(*) FROM context_plans").fetchone() == (
        0,
    )


def test_import_rejects_runtime_input_hash_even_when_sql_check_is_bypassed(
    registry_db, snapshot, import_snapshot
):
    import_snapshot()
    _saver, session, connection, _accepted = registry_db
    connection.execute("PRAGMA ignore_check_constraints=ON")
    with connection:
        connection.execute("UPDATE context_plans SET seal_input_hash='runtime-only'")
    connection.execute("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(ValueError, match="source-mismatch"):
        read_imported_registration(
            connection, session, snapshot.thread_id, snapshot.plan_id
        )


def test_fork_import_requires_source_audit(registry_db, provenance, import_snapshot):
    connection = registry_db[2]
    with connection:
        connection.execute(
            "DELETE FROM fork_identity_mappings WHERE fork_id=? AND entity_type='assembly'",
            (provenance["audit_id"],),
        )
    with pytest.raises(ValueError, match="source snapshot 证据"):
        import_snapshot()
    assert connection.execute("SELECT count(*) FROM context_plans").fetchone() == (0,)
