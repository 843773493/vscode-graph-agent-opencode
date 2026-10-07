"""来源/工具/epoch 统一封存的持久值合同。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.prefix_epoch import (
    AppendedItemRef,
    EpochReason,
    PendingPrefixEpochTransition,
    ProviderProfileItemFrame,
    append_items,
    initial_epoch_state,
    open_rebuild_epoch,
    seal_assembly,
)
from app.domain.itemized.epoch.root_compilation import (
    RootCandidateSource,
    compile_root_item,
)
from app.domain.itemized.epoch.seal_binding import (
    AppliedSourceRevision,
    ContextSealBinding,
)
from app.domain.itemized.epoch.tool_set import (
    ToolSetSnapshot,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.refs import ToolSetRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serde.epoch import (
    context_seal_binding_from_dict,
    context_seal_binding_to_dict,
)


@pytest.fixture
def tool_set() -> ToolSetSnapshot:
    return ToolSetSnapshot(
        selection_revision=sha256_jcs({"read_file": True}),
        tools=({
            "type": "function",
            "function": {
                "name": "read_file", "description": "读文件",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },),
        policy_keys=("filesystem_rules",),
    )


@pytest.fixture
def binding(tool_set: ToolSetSnapshot) -> ContextSealBinding:
    ref = tool_set.bind(session_id="session", thread_id="thread", plan_id="plan")
    state = initial_epoch_state(
        provider_profile="openai-chat:v1",
        tool_compatibility_key=tool_set.compatibility_key(ref),
    )
    root = compile_root_item(state, (RootCandidateSource(
        source_identity="instruction:R01", source_kind="agent_instructions",
        name="Agent 说明", revision=sha256_jcs("说明"), content="本地编码助手",
        source_ordinal=0, root_placement="root_eligible", included_reason="initial",
    ),))
    frame = ProviderProfileItemFrame(
        1, state.provider_profile, "system", "text", root.content,
        AppendedItemRef("source_item", "root:1", "root:1", root.frame_hash(), "tracked"),
    )
    state = append_items(
        state, (frame,), desired_provider_profile=state.provider_profile,
        desired_tool_compatibility_key=state.tool_compatibility_key,
    )
    return ContextSealBinding(
        prefix=seal_assembly(state, assembly_id="assembly"), root=root,
        tool_set_snapshot_id=tool_set.snapshot_id,
        applied_toolset_revision=tool_set.revision,
        selection_revision=tool_set.selection_revision, previous_assembly_id=None,
        source_revisions=(AppliedSourceRevision("instruction:R01", root.sources[0].revision, "decision:1"),),
    )


def test_sealed_binding_preserves_root_and_prefix_after_json_restart(binding):
    restored = context_seal_binding_from_dict(json.loads(json.dumps(
        context_seal_binding_to_dict(binding), ensure_ascii=False,
    )))
    assert restored == binding
    assert restored.prefix.state.epoch_applied
    assert restored.root.sources[0].source_identity == "instruction:R01"
    assert restored.prefix.prefix_hash == binding.prefix.prefix_hash


@pytest.mark.parametrize("field", ["root", "selection_revision", "previous_assembly_id"])
def test_missing_control_field_is_rejected_without_defaults(binding, field):
    raw = context_seal_binding_to_dict(binding)
    del raw[field]
    with pytest.raises(ItemSchemaError, match="字段"):
        context_seal_binding_from_dict(raw)


def test_restored_prefix_cannot_claim_changed_wire_bytes(binding):
    raw = context_seal_binding_to_dict(binding)
    raw["prefix"]["state"]["frames"][0]["content"] = "伪造说明"
    with pytest.raises(ItemSchemaError, match="摘要"):
        context_seal_binding_from_dict(raw)


def test_root_cannot_bind_another_epoch(binding):
    with pytest.raises(ItemSchemaError, match="root"):
        replace(binding, root=replace(binding.root, provider_profile="other-provider"))


def test_tool_snapshot_is_copied_and_owner_independent(tool_set):
    one = tool_set.bind(session_id="session", thread_id="thread", plan_id="plan-1")
    two = tool_set.bind(session_id="session", thread_id="child", plan_id="plan-2")
    assert one.ref_id == two.ref_id
    assert tool_set.compatibility_key(one) == tool_set.compatibility_key(two)
    changed_selection = ToolSetSnapshot(
        selection_revision=sha256_jcs({"hidden_tool": False}),
        tools=tool_set.tools, policy_keys=tool_set.policy_keys,
    )
    assert changed_selection.selection_revision != tool_set.selection_revision
    assert changed_selection.revision == tool_set.revision
    tool_set.tools[0]["function"]["description"] = "外部修改"
    assert tool_set.tools[0]["function"]["description"] == "读文件"


def test_same_epoch_reuses_root_and_extends_parent_bytes(binding):
    state = append_items(
        binding.prefix.state,
        (ProviderProfileItemFrame(
            2, binding.prefix.state.provider_profile, "user", "text", "继续",
            AppendedItemRef("canonical_item", "user-1"),
        ),),
        desired_provider_profile=binding.prefix.state.provider_profile,
        desired_tool_compatibility_key=binding.prefix.state.tool_compatibility_key,
    )
    next_binding = replace(
        binding, prefix=seal_assembly(state, assembly_id="assembly-2"),
        previous_assembly_id="assembly", source_revisions=(),
    )
    restored = context_seal_binding_from_dict(context_seal_binding_to_dict(next_binding))
    assert restored.prefix.parent.assembly_id == "assembly"
    assert restored.root == binding.root
    assert restored.prefix.prefix_byte_length > binding.prefix.prefix_byte_length


@pytest.mark.parametrize("reason", [EpochReason.COMPACTION, EpochReason.REWIND])
def test_rebuild_transition_is_applied_only_by_first_successful_seal(binding, reason):
    transition = PendingPrefixEpochTransition("transition:1", reason)
    state = open_rebuild_epoch(
        binding.prefix.state, reason=reason, transition=transition,
        provider_profile=binding.prefix.state.provider_profile,
        tool_compatibility_key=binding.prefix.state.tool_compatibility_key,
    )
    assert not state.epoch_applied and not transition.consumed
    next_binding = replace(
        binding, prefix=seal_assembly(state, assembly_id="assembly-rebuild", transition=transition),
        root=None, previous_assembly_id="assembly", source_revisions=(),
    )
    restored = context_seal_binding_from_dict(context_seal_binding_to_dict(next_binding))
    assert restored.prefix.state.epoch.ordinal == 2
    assert restored.prefix.consumed_transition.consumed
    assert not transition.consumed


@pytest.fixture
def snapshot(binding, tool_set):
    tool_ref = tool_set.bind(session_id="session", thread_id="thread", plan_id="plan")
    entry = ContextSelectionEntry(
        assembly_id="assembly", plan_ordinal=0, ref=replace(tool_ref, assembly_id="assembly"),
        selection_kind="tool_set", included=True,
        source_revision=tool_ref.source_revision,
        content_hash=tool_ref.content_hash, content_length=tool_ref.content_length,
    )
    plan = ContextRequestPlan(
        session_id="session", thread_id="thread", plan_id="plan", refs=(),
        tool_set_refs=(tool_ref,), tool_snapshot=tool_set.tools,
    ).seal_for_assembly("assembly", selection=(entry,))
    return ContextAssemblySnapshot(
        assembly_id="assembly", session_id="session", thread_id="thread",
        turn_id=None, execution_id="execution", plan_id="plan", plan_hash=plan.plan_hash(),
        request_hash=context_request_hash(plan, "provider", target_format="chat_completions"),
        history_view_revision=0, source_overlay_epoch=0, refs=(), contributions=(),
        tool_snapshot=tool_set.tools, compiler_version=plan.compiler_version, provider_version="provider",
        sealed=True, tool_set_refs=plan.tool_set_refs, selection=plan.selection,
        target_format="chat_completions", seal_binding=binding,
    )


def test_snapshot_serializes_the_same_applied_binding(snapshot, binding):
    restored = ContextAssemblySnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict())))
    assert restored.seal_binding == binding
    restored.validate_hashes()
    with pytest.raises(ItemSchemaError, match="applied binding"):
        replace(snapshot, seal_binding=replace(
            binding, applied_toolset_revision=sha256_jcs("different"),
        ))


@pytest.fixture
def tampered_binding(binding):
    """保持 ToolSet identity，却共同改写 root/prefix 并重算帧摘要。"""
    changed_key = sha256_jcs({"forged_toolset": "same id and revision"})
    raw = context_seal_binding_to_dict(binding)
    raw["prefix"]["state"]["tool_compatibility_key"] = changed_key
    raw["root"]["tool_compatibility_key"] = changed_key
    root = replace(binding.root, tool_compatibility_key=changed_key)
    raw["prefix"]["state"]["frames"][0]["item_ref"]["source_revision"] = root.frame_hash()
    # compatibility key 不进入 frame bytes；重算 root provenance 足以通过
    # binding 自身校验，assembly 必须用真正的 ToolSet manifest 拒绝它。
    return context_seal_binding_from_dict(raw)


def test_snapshot_rejects_cross_binding_tamper(snapshot, tampered_binding):
    assert tampered_binding.applied_toolset_revision == snapshot.seal_binding.applied_toolset_revision
    assert tampered_binding.tool_set_snapshot_id == snapshot.seal_binding.tool_set_snapshot_id
    with pytest.raises(ItemSchemaError, match="compatibility key.*ToolSet"):
        replace(snapshot, seal_binding=tampered_binding)
    raw = snapshot.to_dict()
    raw["seal_binding"] = context_seal_binding_to_dict(tampered_binding)
    with pytest.raises(ItemSchemaError, match="compatibility key.*ToolSet"):
        ContextAssemblySnapshot.from_dict(raw)


def test_validate_hashes_rechecks_toolset_cross_binding(snapshot, tampered_binding):
    # 独立模拟构造后内存篡改，避免仅以构造器拒绝冒充 seal 的最终校验。
    object.__setattr__(snapshot, "seal_binding", tampered_binding)
    with pytest.raises(ItemSchemaError, match="compatibility key.*ToolSet"):
        snapshot.validate_hashes()


@pytest.fixture(params=["protected", "redacted"])
def protected_snapshot(snapshot, request):
    """访问分类不改变内部 ToolSet 正文、精确 hash 或封存 binding。"""
    ref = replace(snapshot.tool_set_refs[0], protection=request.param)
    entry = replace(snapshot.selection[0], ref=ref, protection=request.param)
    plan = ContextRequestPlan(
        session_id=snapshot.session_id, thread_id=snapshot.thread_id,
        plan_id=snapshot.plan_id, refs=(),
        tool_set_refs=(replace(ref, assembly_id=None),),
        tool_snapshot=snapshot.tool_snapshot,
    ).seal_for_assembly(snapshot.assembly_id, selection=(entry,))
    return replace(
        snapshot, tool_set_refs=plan.tool_set_refs, selection=plan.selection,
        plan_hash=plan.plan_hash(),
        request_hash=context_request_hash(
            plan, snapshot.provider_version, target_format=snapshot.target_format,
        ),
    )


def test_sensitive_toolset_roundtrip_preserves_exact_internal_manifest(protected_snapshot):
    raw = protected_snapshot.to_dict()
    assert raw["tool_snapshot"]
    assert raw["tool_set_refs"][0]["tool_policy"] == {
        "enabled_policy_keys": ["filesystem_rules"], "wire_tool_order": ["read_file"],
    }
    assert raw["tool_set_refs"][0]["tools"] == list(protected_snapshot.tool_set_refs[0].tools)
    restored = ContextAssemblySnapshot.from_dict(json.loads(json.dumps(raw)))
    assert restored.seal_binding == protected_snapshot.seal_binding
    restored.validate_hashes()




@pytest.mark.parametrize("changed_field", ["tool_policy", "tools"])
def test_changed_manifest_cannot_keep_toolset_identity_and_applied_binding(snapshot, changed_field):
    original = snapshot.tool_set_refs[0]
    tools = original.tools
    policy = original.tool_policy
    if changed_field == "tool_policy":
        policy = {"enabled_policy_keys": []}
    else:
        tools = ({
            "type": "function",
            "function": {"name": "read_file", "description": "不同的读文件合同"},
        },)
    changed = ToolSetRef.from_tool_snapshot(
        snapshot_id=original.ref_id, session_id=original.session_id,
        thread_id=original.thread_id, plan_id=original.plan_id,
        source_revision=original.source_revision, tools=tools,
        tool_policy=policy, assembly_id=original.assembly_id,
    )
    changed.validate_manifest()
    assert changed.ref_id == original.ref_id
    assert changed.source_revision == original.source_revision
    assert changed.content_hash != original.content_hash
    entry = replace(
        snapshot.selection[0], ref=changed, content_hash=changed.content_hash,
        content_length=changed.content_length,
    )
    plan = ContextRequestPlan(
        session_id=snapshot.session_id, thread_id=snapshot.thread_id,
        plan_id=snapshot.plan_id, refs=(),
        tool_set_refs=(replace(changed, assembly_id=None),), tool_snapshot=tools,
    ).seal_for_assembly(snapshot.assembly_id, selection=(entry,))
    request_hash = context_request_hash(
        plan, snapshot.provider_version, target_format=snapshot.target_format,
    )
    with pytest.raises(ItemSchemaError, match="compatibility key.*ToolSet"):
        replace(
            snapshot, tool_snapshot=tools, tool_set_refs=plan.tool_set_refs,
            selection=plan.selection, plan_hash=plan.plan_hash(),
            request_hash=request_hash,
        )
    raw = snapshot.to_dict()
    raw.update(
        tool_snapshot=list(tools), tool_set_refs=[changed.to_dict()],
        selection=[entry.to_dict()], plan_hash=plan.plan_hash(), request_hash=request_hash,
    )
    with pytest.raises(ItemSchemaError, match="compatibility key.*ToolSet"):
        ContextAssemblySnapshot.from_dict(raw)
