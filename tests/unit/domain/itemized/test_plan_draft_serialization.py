"""草稿恢复保留 registry 身份，拒绝假 seal、跨 owner 和源内容漂移。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.domain.itemized.serde.plan import unsealed_context_plan_from_dict


@pytest.fixture
def draft() -> ContextRequestPlan:
    body = {"instructions": "保留草稿来源", "weight": 0.0000001}
    contribution = ContextContribution(
        contribution_id="contribution", source_kind="workspace_instructions",
        source_revision="revision-1", content_hash=contribution_content_hash("prompt", body),
        body=body, source_ordinal=0,
    )
    ref = ContextRef.request_only_ref(
        "plan-item", session_id="session", plan_id="plan", source_revision="revision-1",
        content_hash_value=contribution.content_hash, content_length=contribution.content_length,
        source_ref=DetailRef("session", "source-assembly", "source-detail"),
    )
    tool = ToolSetRef.from_tool_snapshot(
        session_id="session", plan_id="plan", snapshot_id="tools", source_revision="tools-1",
        tools=({"name": "read_file", "parameters": {"type": "object"}},),
    )
    return ContextRequestPlan(
        session_id="session", plan_id="plan", refs=(ref,), contributions=(contribution,),
        tool_set_refs=(tool,), plan_creation_idempotency_key="creation-key",
    )


def test_draft_roundtrip_preserves_owner_and_deferred_binding(draft):
    raw = json.loads(canonical_json_bytes(draft.to_dict()))
    restored = unsealed_context_plan_from_dict(raw)
    assert restored == draft
    assert restored.assembly_id is None
    assert restored.selection == ()
    assert restored.tool_set_refs[0].assembly_id is None
    assert restored.contributions[0].contribution_ordinal is None
    assert restored.refs[0].source_ref == DetailRef("session", "source-assembly", "source-detail")
    assert "detail_ref" not in restored.refs[0].to_dict()


@pytest.mark.parametrize("field", [
    "session_id", "format_version", "plan_id", "plan_state", "assembly_id",
    "history_view_revision", "source_overlay_epoch", "active_view_id", "selection_policy",
    "refs", "tool_set_refs", "contributions", "selection", "compiler_version",
    "plan_creation_idempotency_key", "plan_hash",
])
def test_draft_restore_rejects_missing_fields(draft, field):
    raw = draft.to_dict()
    del raw[field]
    with pytest.raises(ItemSchemaError, match="draft manifest 字段"):
        unsealed_context_plan_from_dict(raw)


@pytest.mark.parametrize(("field", "value"), [
    ("plan_state", "sealed"), ("assembly_id", "assembly"), ("selection", [{"plan_ordinal": 0}]),
])
def test_draft_restore_cannot_claim_sealed_selection(draft, field, value):
    raw = {**draft.to_dict(), field: value}
    with pytest.raises(ItemSchemaError, match="unsealed plan"):
        unsealed_context_plan_from_dict(raw)


@pytest.mark.parametrize("registry", ["refs", "tool_set_refs", "contributions"])
def test_draft_restore_rejects_duplicate_registry(draft, registry):
    raw = draft.to_dict()
    raw[registry].append(raw[registry][0])
    with pytest.raises(ItemSchemaError, match="重复"):
        unsealed_context_plan_from_dict(raw)


@pytest.mark.parametrize("registry", ["refs", "tool_set_refs"])
@pytest.mark.parametrize("owner", ["session_id", "plan_id"])
def test_draft_restore_rejects_cross_owner(draft, registry, owner):
    raw = draft.to_dict()
    raw[registry][0][owner] = "foreign-owner"
    with pytest.raises(ItemSchemaError):
        unsealed_context_plan_from_dict(raw)


@pytest.mark.parametrize("registry", ["contributions", "tool_set_refs"])
def test_draft_restore_rejects_early_assembly_binding(draft, registry):
    raw = draft.to_dict()
    raw[registry][0]["assembly_id"] = "not-sealed"
    with pytest.raises(ItemSchemaError, match="unsealed plan"):
        unsealed_context_plan_from_dict(raw)


@pytest.mark.parametrize("field", ["history_view_revision", "source_overlay_epoch"])
def test_draft_restore_checks_registry_hash(draft, field):
    raw = {**draft.to_dict(), field: 3}
    with pytest.raises(ItemSchemaError, match="plan-hash-mismatch"):
        unsealed_context_plan_from_dict(raw)


def test_digest_only_draft_never_restores_plaintext(draft):
    digest = "hmac-sha256:session:v1:" + "a" * 64
    contribution = replace(
        draft.contributions[0], body=None, content_hash=None, redacted_stable_digest=digest,
        protection="protected",
    )
    ref = replace(
        draft.refs[0], content_hash=None, redacted_stable_digest=digest, protection="protected",
    )
    protected = replace(draft, refs=(ref,), contributions=(contribution,))
    raw = protected.to_dict()
    assert raw["contributions"][0]["body"] is None
    restored = unsealed_context_plan_from_dict(raw)
    assert restored.contributions[0].body is None
    assert restored.contributions[0].redacted_stable_digest == digest


def test_draft_source_ref_rejects_final_detail_field(draft):
    raw = draft.to_dict()
    raw["refs"][0]["detail_ref"] = DetailRef("session", "assembly", "detail").to_dict()
    with pytest.raises(ItemSchemaError, match="未知或不属于 ref"):
        unsealed_context_plan_from_dict(raw)


@pytest.mark.parametrize("registry", ["refs", "contributions", "tool_set_refs"])
@pytest.mark.parametrize("field", ["detail_ref", "plan_ordinal", "future_field"])
def test_draft_registry_rejects_unknown_and_selection_only_fields(draft, registry, field):
    raw = draft.to_dict()
    raw[registry][0][field] = None
    with pytest.raises(ItemSchemaError, match="未知或不属于"):
        unsealed_context_plan_from_dict(raw)
