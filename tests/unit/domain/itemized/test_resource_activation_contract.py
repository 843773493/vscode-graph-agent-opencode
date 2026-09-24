"""ResourceActivationSnapshotRef/ResourceProvenanceRef 的领域合同测试。

锁死 9.1 的核心语义：两个内容 hash 的覆盖范围严格分离，运行 identity 与
captured_at 不进入任何内容 hash，且所有被拒绝的字段形态都有明确错误码。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationContractError,
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
    SourceLineageRef,
    resource_bindings_hash,
)


def lineage(*, revision: str = "raw-rev-1") -> SourceLineageRef:
    return SourceLineageRef(
        lineage_id="lineage-skill-demo",
        derivation_version="v1",
        sources=(("src-skill", revision),),
    )


def binding(
    *,
    ordinal: int = 1,
    boundary: str = "turn",
    lineage_ref: SourceLineageRef | None = None,
    display_uri: str = "boxteam://workspace/ws/resources/skills/demo/SKILL.md",
) -> ResourceProvenanceRef:
    resolved = lineage() if lineage_ref is None else lineage_ref
    return ResourceProvenanceRef(
        resource_id="skill:demo:metadata",
        display_uri=display_uri,
        resource_kind="skills",
        owner_scope="workspace",
        facet="metadata",
        revision="rev-1",
        availability="available",
        content_length=12,
        content_hash=sha256_jcs("body"),
        redacted_stable_digest=None,
        source_lineage_ref=resolved,
        source_lineage_digest=resolved.digest,
        activation_ordinal=ordinal,
        effective_boundary=boundary,
        captured_registry_generation=3,
        snapshot_ref=DetailRef("ses_a", "asm_1", "snap_1"),
    )


def turn_snapshot(**overrides: object) -> ResourceActivationSnapshotRef:
    base: dict[str, object] = {
        "activation_snapshot_id": "turn:ses_a:thr_b:turn_1",
        "snapshot_kind": "turn",
        "activation_policy_revision": "resource-activation-policy:v1:abc",
        "activation_policy_hash": sha256_jcs({"policy": 1}),
        "registry_generation": 3,
        "owner_session_id": "ses_a",
        "owner_thread_id": "thr_b",
        "turn_id": "turn_1",
        "captured_at": "2026-01-01T00:00:00+00:00",
        "bindings": (binding(),),
    }
    base.update(overrides)
    return ResourceActivationSnapshotRef(**base)  # type: ignore[arg-type]


def model_call_snapshot(
    parent: ResourceActivationSnapshotRef,
    model_call_binding: ResourceProvenanceRef,
    **overrides: object,
) -> ResourceActivationSnapshotRef:
    base: dict[str, object] = {
        "activation_snapshot_id": "model_call:call_1",
        "snapshot_kind": "model_call",
        "activation_policy_revision": parent.activation_policy_revision,
        "activation_policy_hash": parent.activation_policy_hash,
        "registry_generation": parent.registry_generation + 1,
        "owner_session_id": parent.owner_session_id,
        "owner_thread_id": parent.owner_thread_id,
        "turn_id": parent.turn_id,
        "captured_at": "2026-01-01T00:00:01+00:00",
        "bindings": (*parent.bindings, model_call_binding),
        "parent": parent,
        "model_call_id": "call_1",
    }
    base.update(overrides)
    return ResourceActivationSnapshotRef(**base)  # type: ignore[arg-type]


def test_snapshot_kind_turn_and_model_call_bind_parent_and_ordinal() -> None:
    turn = turn_snapshot()
    assert turn.snapshot_kind == "turn"
    assert turn.parent is None and turn.parent_turn_snapshot_id is None
    assert turn.model_call_id is None

    call_binding = binding(
        ordinal=2,
        boundary="model_call",
        display_uri="boxteam://workspace/ws/resources/mcp_tool_catalog/cat",
    )
    call = model_call_snapshot(turn, call_binding)
    assert call.snapshot_kind == "model_call"
    assert call.parent_turn_snapshot_id == turn.activation_snapshot_id
    assert call.model_call_id == "call_1"
    # 同一 assembly 允许混合 boundary，由逐 binding 字段区分。
    assert [item.effective_boundary for item in call.bindings] == [
        "turn",
        "model_call",
    ]
    assert call.bindings[: len(turn.bindings)] == turn.bindings


@pytest.mark.parametrize("invalid", ["Turn", "call", "", "turn ", "model-call"])
def test_snapshot_kind_rejects_unknown_values(invalid: str) -> None:
    with pytest.raises(ResourceActivationContractError) as excinfo:
        turn_snapshot(snapshot_kind=invalid)
    assert excinfo.value.code == "resource-activation-schema-invalid"


def test_model_call_snapshot_requires_typed_parent_relation() -> None:
    turn = turn_snapshot()
    call_binding = binding(ordinal=2, boundary="model_call")
    with pytest.raises(ResourceActivationContractError) as excinfo:
        ResourceActivationSnapshotRef(
            activation_snapshot_id="model_call:call_1",
            snapshot_kind="model_call",
            activation_policy_revision=turn.activation_policy_revision,
            activation_policy_hash=turn.activation_policy_hash,
            registry_generation=4,
            owner_session_id=turn.owner_session_id,
            owner_thread_id=turn.owner_thread_id,
            turn_id=turn.turn_id,
            captured_at="2026-01-01T00:00:01+00:00",
            bindings=(*turn.bindings, call_binding),
            model_call_id="call_1",
        )
    assert excinfo.value.code == "resource-activation-parent-invalid"
    with pytest.raises(ResourceActivationContractError) as excinfo:
        model_call_snapshot(turn, call_binding, model_call_id=None)
    assert excinfo.value.code == "resource-activation-parent-invalid"
    with pytest.raises(ResourceActivationContractError) as excinfo:
        turn_snapshot(parent=turn)
    assert excinfo.value.code == "resource-activation-parent-invalid"


def test_model_call_snapshot_must_reuse_parent_binding_bytes() -> None:
    turn = turn_snapshot()
    changed = replace(turn.bindings[0], revision="rev-2")
    call_binding = binding(ordinal=2, boundary="model_call")
    with pytest.raises(ResourceActivationContractError) as excinfo:
        model_call_snapshot(turn, call_binding, bindings=(changed, call_binding))
    assert excinfo.value.code == "resource-activation-parent-invalid"


def test_lineage_only_change_keeps_bindings_hash_but_changes_provenance_hash() -> None:
    """9.1 核心语义：raw 来源变化但选中语义未变时 bindings/plan hash 不抖动。"""

    original = turn_snapshot()
    advanced = turn_snapshot(bindings=(binding(lineage_ref=lineage(revision="raw-rev-2")),))

    assert advanced.bindings_hash == original.bindings_hash
    assert advanced.bindings[0].revision == original.bindings[0].revision
    assert advanced.bindings[0].source_lineage_digest != (
        original.bindings[0].source_lineage_digest
    )
    assert advanced.activation_provenance_hash != original.activation_provenance_hash


def test_policy_and_boundary_changes_only_move_provenance_hash() -> None:
    original = turn_snapshot()
    republished = turn_snapshot(
        activation_policy_revision="resource-activation-policy:v1:def",
        activation_policy_hash=sha256_jcs({"policy": 2}),
        registry_generation=9,
        bindings=(replace(binding(), captured_registry_generation=9),),
    )
    assert republished.bindings_hash == original.bindings_hash
    assert republished.activation_provenance_hash != original.activation_provenance_hash


def test_additional_semantic_selection_changes_bindings_hash() -> None:
    original = turn_snapshot()
    extra = replace(
        binding(),
        resource_id="skill:demo:activation",
        facet="activation",
        activation_ordinal=2,
    )
    widened = turn_snapshot(bindings=(*original.bindings, extra))
    assert widened.bindings_hash != original.bindings_hash
    assert widened.bindings_hash == resource_bindings_hash((*original.bindings, extra))


def test_runtime_identity_and_captured_at_do_not_change_content_hashes() -> None:
    original = turn_snapshot()
    reshaped = turn_snapshot(
        activation_snapshot_id="turn:ses_a:thr_b:turn_2",
        turn_id="turn_2",
        captured_at="2030-12-31T23:59:59+00:00",
    )
    assert reshaped.bindings_hash == original.bindings_hash
    assert reshaped.activation_provenance_hash == original.activation_provenance_hash


def test_model_call_identity_does_not_change_content_hashes() -> None:
    turn = turn_snapshot()
    call_binding = binding(ordinal=2, boundary="model_call")
    first = model_call_snapshot(turn, call_binding)
    second = model_call_snapshot(
        turn,
        call_binding,
        activation_snapshot_id="model_call:call_2",
        model_call_id="call_2",
        captured_at="2030-12-31T23:59:59+00:00",
    )
    assert first.bindings_hash == second.bindings_hash
    assert first.activation_provenance_hash == second.activation_provenance_hash
    # parent 关系进入 provenance hash，但具体 parent id 只作 typed relation。
    parentless = turn.bindings_hash
    assert first.activation_provenance_hash != parentless


def test_ordinal_conflict_is_rejected() -> None:
    duplicate = replace(
        binding(),
        resource_id="skill:demo:activation",
        activation_ordinal=1,
    )
    with pytest.raises(ResourceActivationContractError) as excinfo:
        turn_snapshot(bindings=(binding(), duplicate))
    assert excinfo.value.code == "resource-activation-ordinal-conflict"


def test_bindings_are_normalized_to_activation_ordinal_order() -> None:
    first = binding(ordinal=1)
    second = replace(binding(ordinal=2), resource_id="skill:demo:activation")
    snapshot = turn_snapshot(bindings=(second, first))
    assert [item.activation_ordinal for item in snapshot.bindings] == [1, 2]
    assert snapshot.bindings_hash == resource_bindings_hash((first, second))


def test_snapshot_roundtrip_preserves_hashes_and_typed_refs() -> None:
    turn = turn_snapshot()
    call_binding = binding(ordinal=2, boundary="model_call")
    call = model_call_snapshot(turn, call_binding)
    for snapshot in (turn, call):
        parent = snapshot.parent
        restored = ResourceActivationSnapshotRef.from_dict(
            json.loads(json.dumps(snapshot.to_dict())), parent=parent
        )
        assert restored == snapshot
        assert restored.bindings_hash == snapshot.bindings_hash
        assert restored.activation_provenance_hash == snapshot.activation_provenance_hash
        ref = restored.bindings[0].snapshot_ref
        assert isinstance(ref, DetailRef)
    assert call.to_dict()["parent_turn_snapshot_id"] == turn.activation_snapshot_id
    assert turn.to_dict()["parent_turn_snapshot_id"] is None


def test_restore_requires_parent_relation_and_matching_id() -> None:
    turn = turn_snapshot()
    call = model_call_snapshot(turn, binding(ordinal=2, boundary="model_call"))
    raw = json.loads(json.dumps(call.to_dict()))
    with pytest.raises(ResourceActivationContractError) as excinfo:
        ResourceActivationSnapshotRef.from_dict(raw)
    assert excinfo.value.code == "resource-activation-parent-invalid"
    raw["parent_turn_snapshot_id"] = "turn:elsewhere"
    with pytest.raises(ResourceActivationContractError) as excinfo:
        ResourceActivationSnapshotRef.from_dict(raw, parent=turn)
    assert excinfo.value.code == "resource-activation-parent-invalid"


def test_restore_rejects_tampered_hashes() -> None:
    raw = json.loads(json.dumps(turn_snapshot().to_dict()))
    for name in ("bindings_hash", "activation_provenance_hash"):
        tampered = {**raw, name: sha256_jcs({"tampered": name})}
        with pytest.raises(ResourceActivationContractError) as excinfo:
            ResourceActivationSnapshotRef.from_dict(tampered)
        assert excinfo.value.code == "resource-activation-hash-mismatch"


def test_lineage_digest_mismatch_is_rejected() -> None:
    with pytest.raises(ResourceActivationContractError) as excinfo:
        replace(binding(), source_lineage_digest=sha256_jcs({"forged": 1}))
    assert excinfo.value.code == "resource-activation-hash-mismatch"
    raw = json.loads(json.dumps(binding().to_dict()))
    raw["source_lineage_ref"]["digest"] = sha256_jcs({"forged": 1})
    with pytest.raises(ResourceActivationContractError) as excinfo:
        ResourceProvenanceRef.from_dict(raw)
    assert excinfo.value.code == "resource-activation-hash-mismatch"


def test_binding_requires_exactly_one_typed_snapshot_or_detail_ref() -> None:
    with pytest.raises(ResourceActivationContractError) as excinfo:
        replace(
            binding(),
            snapshot_ref=DetailRef("ses_a", "asm_1", "snap_1"),
            detail_ref=DetailRef("ses_a", "asm_1", "detail_1"),
        )
    assert excinfo.value.code == "resource-activation-schema-invalid"
    with pytest.raises(ResourceActivationContractError) as excinfo:
        replace(binding(), snapshot_ref=None)
    assert excinfo.value.code == "resource-activation-schema-invalid"
    with pytest.raises(ResourceActivationContractError) as excinfo:
        replace(binding(), snapshot_ref="snap_1")
    assert excinfo.value.code == "resource-activation-schema-invalid"


SNAPSHOT_REJECTIONS = [
    ({}, "resource-activation-boundary-singularity-rejected", "boundary"),
    (
        {},
        "resource-activation-boundary-singularity-rejected",
        "assembly_effective_boundary",
    ),
    ({}, "resource-activation-provider-locator-rejected", "provider_locator"),
    ({}, "resource-activation-provider-locator-rejected", "endpoint"),
    ({}, "resource-activation-credential-rejected", "credential_ref"),
    ({}, "resource-activation-credential-rejected", "api_key"),
    ({}, "resource-activation-absolute-path-rejected", "path"),
    ({}, "resource-activation-absolute-path-rejected", "locator"),
    ({}, "resource-activation-legacy-field-rejected", "read_file_path"),
    ({}, "resource-activation-legacy-field-rejected", "metadata"),
    ({}, "resource-activation-field-alias-rejected", "snapshot_id"),
    ({}, "resource-activation-field-alias-rejected", "created_at"),
]


@pytest.mark.parametrize(("_marker", "code", "field"), SNAPSHOT_REJECTIONS)
def test_snapshot_restore_rejects_forbidden_fields(
    _marker: dict[str, object], code: str, field: str
) -> None:
    raw = json.loads(json.dumps(turn_snapshot().to_dict()))
    raw[field] = "value"
    with pytest.raises(ResourceActivationContractError) as excinfo:
        ResourceActivationSnapshotRef.from_dict(raw)
    assert excinfo.value.code == code


PROVENANCE_REJECTIONS = [
    ("resource-activation-boundary-singularity-rejected", "boundary"),
    ("resource-activation-provider-locator-rejected", "provider_locator"),
    ("resource-activation-provider-locator-rejected", "server_id"),
    ("resource-activation-credential-rejected", "access_token"),
    ("resource-activation-absolute-path-rejected", "absolute_path"),
    ("resource-activation-legacy-field-rejected", "skill_path"),
    ("resource-activation-field-alias-rejected", "semantic_revision"),
    ("resource-activation-field-alias-rejected", "uri"),
]


@pytest.mark.parametrize(("code", "field"), PROVENANCE_REJECTIONS)
def test_provenance_restore_rejects_forbidden_fields(code: str, field: str) -> None:
    raw = json.loads(json.dumps(binding().to_dict()))
    raw[field] = "value"
    with pytest.raises(ResourceActivationContractError) as excinfo:
        ResourceProvenanceRef.from_dict(raw)
    assert excinfo.value.code == code


def test_unregistered_field_is_schema_invalid() -> None:
    raw = json.loads(json.dumps(turn_snapshot().to_dict()))
    raw["extra_field"] = 1
    with pytest.raises(ResourceActivationContractError) as excinfo:
        ResourceActivationSnapshotRef.from_dict(raw)
    assert excinfo.value.code == "resource-activation-schema-invalid"


@pytest.mark.parametrize("value", ["value"])
@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("display_uri", "resource-activation-provider-locator-rejected"),
        ("resource_id", "resource-activation-absolute-path-rejected"),
    ],
)
def test_provenance_value_shape_rejections(field: str, code: str, value: str) -> None:
    hostile = {
        "display_uri": "https://provider.internal/resources/skills/demo",
        "resource_id": "/home/agent/.boxteams/skills/demo/SKILL.md",
    }[field]
    with pytest.raises(ResourceActivationContractError) as excinfo:
        replace(binding(), **{field: hostile})
    assert excinfo.value.code in {code, "resource-activation-legacy-field-rejected"}


def test_provenance_rejects_credential_shaped_identity_and_uri() -> None:
    with pytest.raises(ResourceActivationContractError) as excinfo:
        replace(binding(), resource_id="user@host")
    assert excinfo.value.code == "resource-activation-credential-rejected"
    with pytest.raises(ResourceActivationContractError) as excinfo:
        replace(
            binding(),
            display_uri="boxteam://token@workspace/ws/resources/skills/demo/SKILL.md",
        )
    assert excinfo.value.code == "resource-activation-credential-rejected"


def test_legacy_path_uri_is_rejected() -> None:
    with pytest.raises(ResourceActivationContractError) as excinfo:
        replace(
            binding(),
            display_uri="boxteam://workspace/ws/.boxteam/skills/demo/SKILL.md",
        )
    assert excinfo.value.code == "resource-activation-legacy-field-rejected"


def test_lineage_ref_rejects_duplicate_source_and_locator_fields() -> None:
    with pytest.raises(ResourceActivationContractError) as excinfo:
        SourceLineageRef(
            lineage_id="lineage-1",
            derivation_version="v1",
            sources=(("src-1", "rev-1"), ("src-1", "rev-2")),
        )
    assert excinfo.value.code == "resource-activation-lineage-invalid"
    raw = {**json.loads(json.dumps(lineage().to_dict())), "provider_locator": "x"}
    with pytest.raises(ResourceActivationContractError) as excinfo:
        SourceLineageRef.from_dict(raw)
    assert excinfo.value.code == "resource-activation-provider-locator-rejected"


def test_turn_snapshot_rejects_model_call_bound_binding() -> None:
    with pytest.raises(ResourceActivationContractError) as excinfo:
        turn_snapshot(bindings=(binding(boundary="model_call"),))
    assert excinfo.value.code == "resource-activation-schema-invalid"


def test_snapshot_requires_at_least_one_binding() -> None:
    with pytest.raises(ResourceActivationContractError) as excinfo:
        turn_snapshot(bindings=())
    assert excinfo.value.code == "resource-activation-schema-invalid"
