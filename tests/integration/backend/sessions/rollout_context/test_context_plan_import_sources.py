"""import v2 source manifest 的实际 SQLite、omitted binding 与安全恢复边界。"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace

import pytest

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.request_hash import context_request_hash
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
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    draft as draft,  # noqa: PLC0414 - 各测试文件独立正式工作区
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    import_snapshot as import_snapshot,  # noqa: PLC0414 - 显式注入实际 import port
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    provenance as provenance,  # noqa: PLC0414 - 明确来源指纹
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    registry_db as registry_db,  # noqa: PLC0414 - 按 request.node.path 隔离 SQLite
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    snapshot as snapshot,  # noqa: PLC0414 - 明确 sealed manifest
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    source_manifest as source_manifest,  # noqa: PLC0414 - 明确无正文来源
)


@pytest.fixture
def known_omitted(registry_db, draft):
    owner, session, _connection, accepted = registry_db
    candidate = replace(draft.contributions[0], body=None)
    source = replace(
        draft,
        refs=(replace(draft.refs[0], availability="unavailable"),),
        contributions=(candidate,), tool_set_refs=(),
    )
    result = ContextPlanComposer().assembly(
        plan=source, session_id=session, assembly_id="known-omitted-assembly",
        turn_id=accepted["turn_id"], execution_id=accepted["initial_execution_id"],
        provider_version="import-source-boundary",
    )
    result = replace(
        result, selection=(replace(result.selection[0], contribution_id=candidate.contribution_id),),
    )
    sealed = result.as_sealed_plan()
    result = replace(
        result, plan_hash=sealed.plan_hash(),
        request_hash=context_request_hash(
            sealed, result.provider_version, projector_id=result.projector_id,
            projector_version=result.projector_version, target_format=result.target_format,
            wire_request=result.request_hash_preimage,
        ),
    )
    return result, build_source_manifest(
        session_id=session, plan_id=source.plan_id, refs=source.refs,
        contributions=source.contributions,
    ), owner


@pytest.fixture
def import_known_omitted(known_omitted, import_snapshot):
    value, sources, _owner = known_omitted

    def write(*, manifest=sources):
        import_snapshot(
            value=value, sources=manifest,
            source={
                "source_session_id": value.session_id,
                "source_plan_id": value.plan_id,
                "source_assembly_id": value.assembly_id,
                "source_snapshot_hash": sha256_jcs(value.to_dict()),
                "source_schema_version": 3,
                "audit_id": "known-omitted-import",
            },
        )

    return write


def test_known_omitted_source_roundtrip_keeps_unallocated_manifest(
    registry_db, known_omitted, import_known_omitted, monkeypatch
):
    snapshot, sources, owner = known_omitted

    def no_body(*args, **kwargs):
        pytest.fail("omitted import/read 不得读取 detail 正文或 session contribution registry")

    monkeypatch.setattr(owner._detail_store, "read", no_body)
    connection = registry_db[2]

    def deny_session_registry(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ and table == "context_contributions":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_session_registry)
    try:
        import_known_omitted()
        restored = read_imported_registration(connection, snapshot.session_id, snapshot.plan_id)
    finally:
        connection.set_authorizer(None)
    assert restored.source_manifest == sources
    assert restored.draft is None and restored.creation_hash is None
    assert snapshot.contributions == ()
    assert snapshot.selection[0].contribution_id == sources["contributions"][0]["contribution_id"]
    assert snapshot.selection[0].contribution_ordinal is None
    assert snapshot.selection[0].detail_ref is None
    rows = registry_db[2].execute("SELECT manifest_json FROM context_plan_contributions").fetchall()
    assert [json.loads(row[0]) for row in rows] == sources["contributions"]
    assert all(item["body"] is item["assembly_id"] is item["contribution_ordinal"] is None
               for item in sources["contributions"])


@pytest.mark.parametrize("mutation", [
    "unknown_id", "wrong_ref", "wrong_revision", "wrong_length", "wrong_hash",
    "wrong_visibility", "wrong_protection", "duplicate",
])
def test_omitted_binding_requires_existing_matching_source(
    registry_db, known_omitted, import_known_omitted, mutation
):
    sources = deepcopy(known_omitted[1])
    item = sources["contributions"][0]
    if mutation == "unknown_id":
        item["contribution_id"] = "other-source"
    elif mutation == "wrong_ref":
        item["metadata"]["source_ref"] = "other-ref"
    elif mutation == "wrong_revision":
        item["source_revision"] = "other-revision"
    elif mutation == "wrong_length":
        item["content_length"] += 1
    elif mutation == "wrong_hash":
        item["content_hash"] = sha256_jcs("other content")
    elif mutation == "wrong_visibility":
        item["visibility"] = "private"
    elif mutation == "wrong_protection":
        item["protection"] = "protected"
    else:
        sources["contributions"].append(deepcopy(item))
    with pytest.raises(ValueError, match="source-mismatch|plan-order-integrity"):
        import_known_omitted(manifest=sources)
    assert registry_db[2].execute("SELECT count(*) FROM context_plans").fetchone() == (0,)


@pytest.mark.parametrize("mutation", [
    "unknown_schema", "wrong_session", "wrong_plan", "duplicate_ref", "missing_ref_field",
    "ref_owner", "extra_ref_field", "body", "assembly", "ordinal", "missing_body_field",
    "metadata_api_key", "metadata_headers", "metadata_unicode", "metadata_nan",
])
def test_source_manifest_writer_rejects_invalid_or_sensitive_values(
    registry_db, source_manifest, import_snapshot, mutation
):
    sources = deepcopy(source_manifest)
    item, ref = sources["contributions"][0], sources["refs"][0]
    if mutation == "unknown_schema":
        sources["schema"] = "context-plan-source:v0"
    elif mutation in {"wrong_session", "wrong_plan"}:
        sources["session_id" if mutation == "wrong_session" else "plan_id"] = "other"
    elif mutation == "duplicate_ref":
        sources["refs"].append(deepcopy(ref))
    elif mutation == "missing_ref_field":
        ref.pop("plan_id")
    elif mutation == "ref_owner":
        ref["session_id"] = "other"
    elif mutation == "extra_ref_field":
        ref["detail_ref"] = None
    elif mutation == "body":
        item["body"] = "never-persist-secret"
    elif mutation == "assembly":
        item["assembly_id"] = "sealed-too-early"
    elif mutation == "ordinal":
        item.update(assembly_id="sealed-too-early", contribution_ordinal=0)
    elif mutation == "missing_body_field":
        item.pop("body")
    elif mutation == "metadata_api_key":
        item["metadata"]["API_KEY"] = "never-persist-secret"
    elif mutation == "metadata_headers":
        item["metadata"]["headers"] = {"pRoXy-AuThOrIzAtIoN": "never-persist-secret"}
    elif mutation == "metadata_unicode":
        item["metadata"]["invalid"] = "\ud800"
    else:
        item["metadata"]["invalid"] = float("nan")
    with pytest.raises(ValueError, match="source-mismatch") as caught:
        import_snapshot(sources=sources)
    assert "never-persist-secret" not in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert registry_db[2].execute("SELECT count(*) FROM context_plans").fetchone() == (0,)


@pytest.mark.parametrize("field", ["draft", "revision", "selection", "assembly_id", "plan_creation_idempotency_key"])
def test_source_manifest_cannot_fabricate_draft_fields(source_manifest, import_snapshot, field):
    with pytest.raises(ValueError, match="source-mismatch"):
        import_snapshot(sources={**source_manifest, field: None})


@pytest.mark.parametrize("protection", ["public", "protected"])
def test_builder_never_strips_inline_body(draft, protection):
    contribution = replace(draft.contributions[0], protection=protection)
    with pytest.raises(ValueError, match="无正文"):
        build_source_manifest(
            session_id=draft.session_id, plan_id=draft.plan_id, refs=draft.refs,
            contributions=(contribution,),
        )
    assert contribution.body == draft.contributions[0].body


@pytest.mark.parametrize("mutation", ["null", "non_jcs", "duplicate_key", "body", "safe_metadata", "index_missing", "index_extra"])
def test_persisted_source_manifest_and_indexes_are_verified(
    registry_db, snapshot, source_manifest, import_snapshot, mutation
):
    import_snapshot()
    connection = registry_db[2]
    value = deepcopy(source_manifest)
    raw = json_text(value)
    if mutation == "null":
        connection.execute("PRAGMA ignore_check_constraints=ON")
        raw = None
    elif mutation == "non_jcs":
        raw = " " + raw
    elif mutation == "duplicate_key":
        raw = '{"schema":"context-plan-source:v1",' + raw[1:]
    elif mutation == "body":
        value["contributions"][0]["body"] = "never-persist-secret"
        raw = json_text(value)
    elif mutation == "safe_metadata":
        value["contributions"][0]["metadata"]["source_ordinal"] += 1
        raw = json_text(value)
    with connection:
        if mutation == "index_missing":
            connection.execute("DELETE FROM context_plan_contributions")
        elif mutation == "index_extra":
            connection.execute(
                "INSERT INTO context_plan_contributions SELECT session_id, plan_id, 'unexpected', manifest_json FROM context_plan_contributions"
            )
        else:
            connection.execute("UPDATE context_plans SET source_manifest_json=?", (raw,))
    connection.execute("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(ValueError, match="source-mismatch") as caught:
        read_imported_registration(connection, snapshot.session_id, snapshot.plan_id)
    assert "never-persist-secret" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_import_v2_binds_even_unselected_source_metadata(snapshot, provenance, source_manifest):
    changed = deepcopy(source_manifest)
    changed["contributions"][0]["metadata"]["source_ordinal"] += 1
    inputs = {"detail_key": None, "origin": "schema3_import", "source_provenance": provenance}
    assert imported_registration_hash(snapshot, source_manifest=source_manifest, **inputs) != (
        imported_registration_hash(snapshot, source_manifest=changed, **inputs)
    )


def test_import_api_has_no_missing_manifest_fallback(registry_db, snapshot, provenance):
    inputs = {"detail_key": None, "origin": "schema3_import", "source_provenance": provenance}
    with pytest.raises(TypeError, match="source_manifest"):
        imported_registration_hash(snapshot, **inputs)
    with pytest.raises(TypeError, match="source_manifest"):
        write_imported_registration(registry_db[2], snapshot, seal_idempotency_key="explicit", **inputs)
