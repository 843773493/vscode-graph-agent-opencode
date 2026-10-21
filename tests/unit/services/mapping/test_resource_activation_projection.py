"""9.4 sealed resource refs 的安全跨运行面投影合同测试。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationPolicySnapshot,
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
    SourceLineageRef,
)
from app.services.mapping.itemized.resource_activation import (
    project_sealed_resource_refs,
)

POLICY = ResourceActivationPolicySnapshot.from_dict({"default_boundary": "turn", "overrides": {}})

SESSION_ID = "ses_019c2b8707cf79388ef1648fa32ee819"


def _binding(
    resource_id: str,
    *,
    ordinal: int,
    boundary: str,
    revision: str = "rev-1",
    availability: str = "available",
) -> ResourceProvenanceRef:
    lineage = SourceLineageRef(
        lineage_id="lineage-1",
        derivation_version="derivation:v1",
        sources=(("source-1", "src-rev-1"),),
    )
    return ResourceProvenanceRef(
        resource_id=resource_id,
        display_uri=f"boxteam://workspace/test/resources/skills/{resource_id.replace(':', '-')}/SKILL.md",
        resource_kind="skills",
        owner_scope="session",
        facet="activation",
        revision=revision,
        availability=availability,
        content_length=7,
        content_hash="sha256:jcs:v1:" + "a" * 64,
        source_lineage_ref=lineage,
        source_lineage_digest=lineage.digest,
        activation_ordinal=ordinal,
        effective_boundary=boundary,
        captured_registry_generation=3,
        snapshot_ref=DetailRef(SESSION_ID, "main", "snap-1", f"body-{ordinal}"),
    )


def _execution_snapshot(*, extra_ordinals: bool = False) -> ResourceActivationSnapshotRef:
    bindings: list[ResourceProvenanceRef] = [
        _binding("skill:alpha", ordinal=0, boundary="turn")
    ]
    if extra_ordinals:
        bindings.append(_binding("skill:beta", ordinal=1, boundary="turn"))
    return ResourceActivationSnapshotRef(
        activation_snapshot_id=f"activation-turn:{SESSION_ID}:main:turn-1",
        snapshot_kind="execution",
        activation_policy_revision=POLICY.revision,
        activation_policy_hash=POLICY.hash,
        activation_policy=POLICY,
        registry_generation=3,
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        execution_id="execution-1",
        turn_id="turn-1",
        captured_at="2026-09-24T00:00:00+00:00",
        bindings=tuple(bindings),
    )


@pytest.mark.parametrize("display_uri", [None, "boxteam://workspace/test/resources/skills/demo/SKILL.md"])
def test_projection_exposes_only_safe_fields(display_uri) -> None:
    snapshot = _execution_snapshot(extra_ordinals=True)
    private_revision = "/abs/private/config?credential=secret"
    snapshot = replace(snapshot, bindings=tuple(
        replace(binding, display_uri=display_uri, revision=private_revision)
        for binding in snapshot.bindings
    ))
    projections = project_sealed_resource_refs(snapshot)
    assert len(projections) == 2
    for projection in projections:
        payload = projection.model_dump(mode="json")
        assert set(payload) == {"display_uri", "resource_kind", "owner_scope", "facet", "revision", "availability", "provenance_ref"}
        assert payload["display_uri"] == display_uri
        assert payload["owner_scope"] == "session"
        assert payload["revision"] == sha256_jcs({
            "schema": "resource-display-revision:v1", "revision": private_revision,
        })
        assert set(payload["provenance_ref"]) == {"kind", "token"}
        assert payload["provenance_ref"]["kind"] == "sealed_resource"
        for private in (private_revision, "snapshot_ref", "detail_ref", "source_lineage", "resource_id", "content_hash", "members", "activation_snapshot_id"):
            assert private not in json.dumps(payload)
    # 同URI仍是两条不同身份，不按display_uri合并。
    assert projections[0].provenance_ref != projections[1].provenance_ref
    binding = snapshot.bindings[0]
    assert projections[0].provenance_ref.token == sha256_jcs({
        "schema": "resource-provenance-ref:v1",
        "owner_session_id": SESSION_ID,
        "owner_thread_id": "main",
        "activation_snapshot_id": snapshot.activation_snapshot_id,
        "resource_id": binding.resource_id,
        "revision": binding.revision,
    })
    rebuilt = ResourceActivationSnapshotRef.from_dict(snapshot.to_dict())
    assert project_sealed_resource_refs(rebuilt) == projections


def test_projection_preserves_sealed_order_and_revision_identity() -> None:
    snapshot = _execution_snapshot(extra_ordinals=True)
    projected = project_sealed_resource_refs(snapshot)
    assert [row.display_uri for row in projected] == [row.display_uri for row in snapshot.bindings]
    changed = replace(snapshot, bindings=(
        replace(snapshot.bindings[0], revision="different-revision"), snapshot.bindings[1],
    ))
    next_rows = project_sealed_resource_refs(changed)
    assert next_rows[0].revision != projected[0].revision
    assert next_rows[0].provenance_ref != projected[0].provenance_ref
    assert next_rows[1] == projected[1]
