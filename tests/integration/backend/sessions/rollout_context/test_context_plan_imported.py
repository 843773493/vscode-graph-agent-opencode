"""真实 schema4 SQLite 与 domain sealed import port；不模拟旧 schema。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.assembly.plans.imported import (
    imported_registration_hash,
    read_imported_registration,
    write_imported_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.imported_sources import (
    build_source_manifest,
)
from app.services.infrastructure.rollout_context.assembly.plans.manifest import (
    json_text,
    sealed_hash,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    read_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.sealing import (
    preflight_seal,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    _insert_snapshot,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    draft as draft,  # noqa: PLC0414 - 显式注入，输出按当前 request.node.path 隔离
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 不运行被导入文件的测试
)


@pytest.fixture
def snapshot(registry_db, draft):
    _saver, session, _connection, accepted = registry_db
    result = ContextPlanComposer().assembly(
        plan=draft,
        assembly_id="imported-assembly",
        session_id=session,
        turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"],
        provider_version="imported-fixture",
        request_detail_refs={
            draft.refs[0].ref_id: DetailRef(session, "imported-assembly", "source")
        },
    )
    return replace(
        result,
        contributions=tuple(replace(item, body=None) for item in result.contributions),
    )


@pytest.fixture
def source_manifest(draft):
    return build_source_manifest(
        session_id=draft.session_id, plan_id=draft.plan_id, refs=draft.refs,
        contributions=tuple(replace(item, body=None) for item in draft.contributions),
    )


@pytest.fixture
def provenance(snapshot):
    return {
        "source_session_id": snapshot.session_id,
        "source_plan_id": snapshot.plan_id,
        "source_assembly_id": snapshot.assembly_id,
        "source_snapshot_hash": sha256_jcs(snapshot.to_dict()),
        "source_schema_version": 3,
        "audit_id": "explicit-upgrade-audit",
    }


@pytest.fixture
def import_snapshot(registry_db, snapshot, provenance, source_manifest):
    _saver, _session, connection, _accepted = registry_db

    def write(
        *, value=snapshot, source=provenance, origin="schema3_import", detail=None,
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


@pytest.mark.parametrize("origin", ["schema3_import", "fork_import"])
def test_import_roundtrip_is_sealed_without_fabricated_draft(
    registry_db, snapshot, provenance, import_snapshot, origin, source_manifest
):
    _saver, session, connection, _accepted = registry_db
    source = dict(provenance)
    if origin == "fork_import":
        source.update(
            source_session_id="source",
            source_plan_id="source-plan",
            source_assembly_id="source-assembly",
            source_schema_version=4,
        )
        with connection:
            connection.execute(
                "INSERT INTO fork_identity_mappings(mapping_id, fork_id, source_session_id, "
                "target_session_id, entity_type, source_local_id, target_local_id, lineage_json, created_at) "
                "VALUES ('fixture-source-audit', ?, 'source', ?, 'assembly', 'source-assembly', ?, ?, 'fixture')",
                (
                    source["audit_id"],
                    session,
                    snapshot.assembly_id,
                    json_text({"source_snapshot": source}),
                ),
            )
    import_snapshot(source=source, origin=origin)
    registered = read_registration(connection, session, snapshot.plan_id)
    assert registered == read_imported_registration(
        connection, session, snapshot.plan_id
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
        "origin": "schema3_import",
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


@pytest.mark.parametrize("mutation", ["extra", "bad_hash", "future", "unknown_origin"])
def test_import_provenance_is_closed(provenance, import_snapshot, mutation):
    source = dict(provenance)
    origin = "schema3_import"
    if mutation == "extra":
        source["source_body"] = "must not persist"
    elif mutation == "bad_hash":
        source["source_snapshot_hash"] = "sha256:jcs:v1:" + "A" * 64
    elif mutation == "future":
        source["source_schema_version"] = 5
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
        read_registration(connection, session, snapshot.plan_id)
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
        read_registration(connection, session, snapshot.plan_id)
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
            origin="schema3_import",
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
            origin="schema3_import",
            source_provenance={**provenance, "audit_id": "other"},
            source_manifest=source_manifest,
            seal_idempotency_key="import-seal",
        )
    assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("owner", ["session", "assembly"])
def test_import_rejects_cross_owner_header_detail(
    registry_db, snapshot, import_snapshot, owner
):
    detail = DetailRef(
        "other" if owner == "session" else snapshot.session_id,
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
            origin="schema3_import",
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
    origin = "schema3_import"
    if field == "origin":
        origin = "fork_import"
        source.update(
            source_schema_version=4,
            source_session_id="other-session",
            source_plan_id="other-plan",
            source_assembly_id="other-assembly",
        )
    else:
        source[field] = (
            sha256_jcs("other") if field == "source_snapshot_hash" else "other"
        )
    with connection:
        connection.execute(
            "UPDATE context_plans SET registration_origin=?, source_provenance_json=?",
            (origin, json_text(source)),
        )
    with pytest.raises(ValueError, match="source-mismatch"):
        read_registration(connection, session, snapshot.plan_id)


@pytest.mark.parametrize(
    "field",
    [
        "source_session_id",
        "source_plan_id",
        "source_assembly_id",
        "source_snapshot_hash",
    ],
)
def test_schema3_import_requires_same_source_snapshot(
    import_snapshot, provenance, field
):
    source = {
        **provenance,
        field: sha256_jcs("other") if field == "source_snapshot_hash" else "other",
    }
    with pytest.raises(ValueError, match="原同一 snapshot"):
        import_snapshot(source=source)


def test_import_hash_namespace_is_independent(snapshot, provenance, source_manifest):
    assert imported_registration_hash(
        snapshot, detail_key=None, origin="schema3_import", source_provenance=provenance,
        source_manifest=source_manifest,
    ) == sha256_jcs(
        {
            "schema": "context-plan-import:v2",
            "registration_origin": "schema3_import",
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
        read_registration(connection, session, snapshot.plan_id)
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
        read_imported_registration(connection, session, snapshot.plan_id)


def test_fork_import_requires_source_audit(registry_db, provenance, import_snapshot):
    source = {
        **provenance,
        "source_schema_version": 4,
        "source_session_id": "source",
        "source_plan_id": "source-plan",
        "source_assembly_id": "source-assembly",
    }
    with pytest.raises(ValueError, match="source snapshot 证据"):
        import_snapshot(source=source, origin="fork_import")
    assert registry_db[2].execute("SELECT count(*) FROM context_plans").fetchone() == (
        0,
    )
