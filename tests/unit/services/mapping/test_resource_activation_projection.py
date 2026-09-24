"""9.4 sealed resource refs 的安全跨运行面投影合同测试。"""

from __future__ import annotations

import json

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
    SourceLineageRef,
)
from app.services.mapping.itemized.resource_activation import (
    SAFE_RESOURCE_PROJECTION_FIELDS,
    ResourceActivationProjectionError,
    assert_resource_bindings_not_counted_as_items,
    project_sealed_resource_refs,
    resource_projection_order_key,
)

SESSION_ID = "ses_2d9b8c7a6f5e4d3c2b1a098765432100"


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
        display_uri=f"boxteam://workspace/test/{resource_id.replace(':', '-')}",
        resource_kind="skills",
        owner_scope="session",
        facet="activation",
        revision=revision,
        availability=availability,
        content_length=7,
        content_hash="sha256:jcs:v1:" + "a" * 64,
        redacted_stable_digest=None,
        source_lineage_ref=lineage,
        source_lineage_digest=lineage.digest,
        activation_ordinal=ordinal,
        effective_boundary=boundary,
        captured_registry_generation=3,
        snapshot_ref=DetailRef(SESSION_ID, "snap-1", f"body-{ordinal}"),
    )


def _turn_snapshot(*, extra_ordinals: bool = False) -> ResourceActivationSnapshotRef:
    bindings: list[ResourceProvenanceRef] = [
        _binding("skill:alpha", ordinal=0, boundary="turn")
    ]
    if extra_ordinals:
        bindings.append(_binding("skill:beta", ordinal=1, boundary="turn"))
    return ResourceActivationSnapshotRef(
        activation_snapshot_id=f"activation-turn:{SESSION_ID}:main:turn-1",
        snapshot_kind="turn",
        activation_policy_revision="resource-activation-policy:v1:abc",
        activation_policy_hash="sha256:jcs:v1:" + "1" * 64,
        registry_generation=3,
        owner_session_id=SESSION_ID,
        owner_thread_id="main",
        turn_id="turn-1",
        captured_at="2026-09-24T00:00:00+00:00",
        bindings=tuple(bindings),
    )


def test_projection_exposes_only_safe_fields() -> None:
    snapshot = _turn_snapshot(extra_ordinals=True)
    projections = project_sealed_resource_refs(snapshot)

    assert len(projections) == 2
    for projection in projections:
        payload = projection.to_dict()
        assert set(payload) == SAFE_RESOURCE_PROJECTION_FIELDS
        assert payload["display_uri"].startswith("boxteam://")
        # opaque provenance ref 不外泄 raw resource_id。
        assert "skill:" not in payload["provenance_ref"]


def test_projection_never_leaks_locator_credential_or_body() -> None:
    snapshot = _turn_snapshot()
    encoded = json.dumps(
        [projection.to_dict() for projection in project_sealed_resource_refs(snapshot)]
    )
    for forbidden in (
        "provider_locator",
        "credential",
        "api_key",
        "access_token",
        ".boxteam",
        "/abs/",
        "file://",
        "detail_id",
        "payload",
    ):
        assert forbidden not in encoded


def test_identity_and_order_are_stable_across_projection_calls() -> None:
    """live/刷新/重启/跨端读取必须得到同一 identity/order。"""

    snapshot = _turn_snapshot(extra_ordinals=True)
    first = project_sealed_resource_refs(snapshot)
    # 模拟重启后从 SQLite/受保护 body 重建出的同一 snapshot。
    rebuilt = ResourceActivationSnapshotRef.from_dict(snapshot.to_dict())
    second = project_sealed_resource_refs(rebuilt)

    assert [item.to_dict() for item in first] == [item.to_dict() for item in second]
    assert resource_projection_order_key(first) == resource_projection_order_key(second)
    assert [item.activation_ordinal for item in first] == [0, 1]


def test_projection_order_key_is_monotonic_in_ordinal() -> None:
    projections = project_sealed_resource_refs(_turn_snapshot(extra_ordinals=True))
    keys = resource_projection_order_key(projections)
    assert [key[0] for key in keys] == sorted(key[0] for key in keys)


def test_request_only_binding_not_counted_as_turn_item() -> None:
    projections = project_sealed_resource_refs(_turn_snapshot())
    # 零 Item Turn 仍可携带 resource binding。
    assert_resource_bindings_not_counted_as_items(
        projections, {"item_count": 0}
    )
    assert_resource_bindings_not_counted_as_items(
        projections,
        {"item_count": 2, "first_item_sequence": 1, "last_item_sequence": 2},
    )


@pytest.mark.parametrize(
    "forbidden_key",
    ["resource_count", "resource_item_count", "binding_count", "resource_elapsed_ms"],
)
def test_resource_count_keys_are_rejected(forbidden_key: str) -> None:
    projections = project_sealed_resource_refs(_turn_snapshot())
    with pytest.raises(ResourceActivationProjectionError) as error:
        assert_resource_bindings_not_counted_as_items(
            projections, {"item_count": 0, forbidden_key: 1}
        )
    assert error.value.code == "resource-activation-projection-invalid"


def test_zero_item_turn_must_not_gain_sequence_range() -> None:
    with pytest.raises(ResourceActivationProjectionError):
        assert_resource_bindings_not_counted_as_items(
            (), {"item_count": 0, "first_item_sequence": 1, "last_item_sequence": 1}
        )


def test_projection_rejects_non_domain_input() -> None:
    with pytest.raises(TypeError, match="ResourceActivationSnapshotRef"):
        project_sealed_resource_refs({"bindings": []})  # type: ignore[arg-type]
