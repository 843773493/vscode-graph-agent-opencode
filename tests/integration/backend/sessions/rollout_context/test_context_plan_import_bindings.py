"""显式 import 不能凭 self-consistent snapshot 补造 omitted contribution binding。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.itemized.hash.request_hash import context_request_hash
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    fork_case as fork_case,  # noqa: PLC0414 - 共享真实 fork fixture 依赖
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    import_snapshot as import_snapshot,  # noqa: PLC0414 - 唯一 import port
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    provenance as provenance,  # noqa: PLC0414 - 共享真实 fork fixture 依赖
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    registry_db as registry_db,  # noqa: PLC0414 - 真实 fork 及当前测试路径隔离
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    snapshot as snapshot,  # noqa: PLC0414 - 真正 target-local sealed snapshot
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_imported import (
    source_manifest as source_manifest,  # noqa: PLC0414 - 共享真实 fork fixture 依赖
)


@pytest.fixture
def fork_plan_kind():
    return "unbound_omitted"


@pytest.fixture
def omitted_snapshot(snapshot):
    return snapshot


@pytest.fixture
def write_omitted_import(import_snapshot):
    def write(snapshot):
        import_snapshot(value=snapshot)

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
