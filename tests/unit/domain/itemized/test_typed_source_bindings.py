"""typed source binding 与非决策扩展的写入/恢复边界。"""

from dataclasses import replace

import pytest

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash
from app.domain.itemized.plans.contributions import (
    ContextContribution,
    SourceBinding,
    parse_contribution_binding_manifest,
)
from app.domain.itemized.serde.registry import parse_contribution


@pytest.fixture(scope="module")
def contribution() -> ContextContribution:
    return ContextContribution(
        contribution_id="source-1",
        source_kind="workspace_instructions",
        source_revision="revision-1",
        content_hash=contribution_content_hash("prompt", "真实来源正文"),
        body="真实来源正文",
        source_binding=SourceBinding(source_ref="source-alias"),
    )


@pytest.mark.parametrize("extensions", [
    {"selection_role": {"extension_version": "1", "value": "backing_only", "protection": "public"}},
    {"vendor.audit": {"extension_version": 1, "value": {}, "protection": "public"}},
    {"vendor.audit": {"extension_version": "", "value": {}, "protection": "public"}},
    {"vendor.audit": {"extension_version": "1", "protection": "public"}},
    {"vendor.audit": {"extension_version": "1", "value": {}, "protection": []}},
    {"vendor.audit": {"extension_version": "1", "value": {}, "protection": "public", "selection_role": "backing_only"}},
])
def test_invalid_extension_envelope_is_rejected(contribution, extensions) -> None:
    with pytest.raises(ItemSchemaError):
        replace(contribution, extensions=extensions)


def test_unknown_extension_values_roundtrip_without_control_power(contribution) -> None:
    unknown = {"vendor.audit": {
        "extension_version": "future-7",
        "protection": "public",
        "encoding": "vendor-json",
        "value": {
            "source_ref": "attacker-alias",
            "source_ordinal": 999,
            "selection_role": "backing_only",
            "replacement_policy": "replaceable",
            "root_placement": "root_eligible",
            "overlay_role": "delta",
            "nested": [{"payload": "保留未知 Unicode 😀"}],
        },
    }}
    original = replace(contribution, extensions=unknown)
    restored = parse_contribution(original.to_dict(), sealed=False)
    assert canonical_json_bytes(restored.extensions) == canonical_json_bytes(unknown)
    assert restored.source_binding == SourceBinding(source_ref="source-alias")
    assert restored.source_ordinal is None
    assert restored.selection_role == "direct"
    assert restored.replacement_policy == "immutable"
    assert restored.root_placement == "tail_only"


@pytest.mark.parametrize("missing", [
    "extensions", "source_binding", "root_placement", "selection_role", "replacement_policy",
])
def test_restore_requires_explicit_core_binding_fields(contribution, missing) -> None:
    raw = contribution.to_dict()
    del raw[missing]
    with pytest.raises(ItemSchemaError):
        parse_contribution(raw, sealed=False)


def test_old_metadata_shape_is_rejected(contribution) -> None:
    raw = contribution.to_dict()
    raw["metadata"] = raw.pop("extensions")
    with pytest.raises(ItemSchemaError):
        parse_contribution(raw, sealed=False)


def test_sql_binding_manifest_cannot_override_registry_columns(contribution) -> None:
    raw = contribution.binding_manifest()
    raw["source_ordinal"] = 999
    with pytest.raises(ItemSchemaError, match="binding manifest"):
        parse_contribution_binding_manifest(raw)


def test_overlay_extension_cannot_supply_missing_source_binding(contribution) -> None:
    with pytest.raises(ItemSchemaError, match="source binding role"):
        replace(
            contribution,
            contribution_kind="overlay_base",
            content_hash=contribution_content_hash("overlay_base", contribution.body),
            source_binding=None,
            extensions={"vendor.overlay": {
                "extension_version": "1", "protection": "public",
                "value": {"source_ref": "alias", "overlay_ref": "alias", "overlay_id": "ov", "overlay_role": "base", "source_overlay_epoch": 0},
            }},
        )
