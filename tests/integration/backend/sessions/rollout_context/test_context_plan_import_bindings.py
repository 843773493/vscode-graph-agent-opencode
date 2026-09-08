"""显式 import 不能凭 self-consistent snapshot 补造 omitted contribution binding。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_hash import context_request_hash
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.assembly.plans.imported import (
    write_imported_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.imported_sources import (
    build_source_manifest,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    _insert_snapshot,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 按当前测试路径独立 fixture
)


@pytest.fixture
def omitted_draft(registry_db):
    _saver, session, _connection, _accepted = registry_db
    return ContextRequestPlan(
        session_id=session,
        plan_id="omitted-import-plan",
        refs=(
            ContextRef.request_only_ref(
                "omitted-ref",
                session_id=session,
                plan_id="omitted-import-plan",
                source_revision="known-revision",
                availability="unavailable",
            ),
        ),
        plan_creation_idempotency_key="source-only-creation",
    )


@pytest.fixture
def omitted_manifest(omitted_draft):
    return build_source_manifest(
        session_id=omitted_draft.session_id, plan_id=omitted_draft.plan_id,
        refs=omitted_draft.refs, contributions=omitted_draft.contributions,
    )


@pytest.fixture
def omitted_snapshot(registry_db, omitted_draft):
    _saver, session, _connection, accepted = registry_db
    return ContextPlanComposer().assembly(
        plan=omitted_draft,
        assembly_id="omitted-import-assembly",
        session_id=session,
        turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"],
        provider_version="binding-regression",
    )


@pytest.fixture
def write_omitted_import(registry_db, omitted_manifest):
    connection = registry_db[2]

    def write(snapshot):
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _insert_snapshot(connection, snapshot)
            write_imported_registration(
                connection,
                snapshot,
                detail_key=None,
                origin="schema3_import",
                source_manifest=omitted_manifest,
                source_provenance={
                    "source_session_id": snapshot.session_id,
                    "source_plan_id": snapshot.plan_id,
                    "source_assembly_id": snapshot.assembly_id,
                    "source_snapshot_hash": sha256_jcs(snapshot.to_dict()),
                    "source_schema_version": 3,
                    "audit_id": "binding-audit",
                },
                seal_idempotency_key="binding-import",
            )

    return write


def test_omitted_ref_without_claimed_binding_can_import(
    omitted_snapshot, write_omitted_import
):
    assert omitted_snapshot.contributions == ()
    assert omitted_snapshot.selection[0].contribution_id is None
    write_omitted_import(omitted_snapshot)


@pytest.mark.parametrize("contribution_id", ["unknown-source", "omitted-ref"])
def test_import_rejects_omitted_binding_without_source_registry_evidence(
    registry_db, omitted_snapshot, write_omitted_import, contribution_id
):
    snapshot = replace(
        omitted_snapshot,
        selection=(
            replace(omitted_snapshot.selection[0], contribution_id=contribution_id),
        ),
    )
    plan = snapshot.as_sealed_plan()
    snapshot = replace(
        snapshot,
        plan_hash=plan.plan_hash(),
        request_hash=context_request_hash(
            plan,
            snapshot.provider_version,
            projector_id=snapshot.projector_id,
            projector_version=snapshot.projector_version,
            target_format=snapshot.target_format,
            wire_request=snapshot.request_hash_preimage,
        ),
    )
    snapshot.validate_hashes()
    assert not snapshot.selection[0].included
    assert snapshot.selection[0].contribution_ordinal is None
    assert snapshot.contributions == ()
    with pytest.raises(ValueError, match="source-mismatch|plan-order-integrity"):
        write_omitted_import(snapshot)
    assert registry_db[2].execute("SELECT count(*) FROM context_plans").fetchone() == (
        0,
    )
