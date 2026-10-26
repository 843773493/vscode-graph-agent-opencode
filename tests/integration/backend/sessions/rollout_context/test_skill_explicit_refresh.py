"""真实 Skill 文件发布 → 冻结工具调用 → 模式切换 → seal 与冷读。"""

import asyncio
import json
from contextlib import nullcontext

from app.agents.tools.skill_loading import create_skill_load_tool
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 使用当前文件的正式隔离工作区
)
from tests.integration.backend.sessions.rollout_context.test_skill_catalog_shadow_binding import (
    WORKSPACE_BODY,
    _write_shared_skill,
)
from tests.integration.backend.sessions.rollout_context.test_skill_catalog_shadow_binding import (
    skill_catalog_owner as skill_catalog_owner,  # noqa: PLC0414 - 真实来源 owner
)


async def test_real_skill_refresh_and_modes_share_sealed_body(
    skill_catalog_owner, frozen_provider_wire, frozen_provider_activation,
):
    saver, session, thread, accepted, registry, files, manager = skill_catalog_owner
    owner = ContextSourceOwnerKey(session, thread)
    admission = saver.execution_admission(session, thread_id=thread, execution_id=accepted["initial_execution_id"])
    policy = ResourceActivationPolicySnapshot(default_boundary="turn", boundaries={"skill": "model_call"})
    tools = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())

    def seal(key):
        with (
            session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            return saver.prepare_context_for_provider(
                session, thread, turn_id=admission.turn_id, execution_id=admission.execution_id,
                plan_creation_idempotency_key=key, seal_idempotency_key="seal:" + key,
                context_source_manager=manager, tool_set_supplier=lambda: tools,
                **frozen_provider_wire("skill-refresh", "chat_completions"),
                **frozen_provider_activation(saver, plan_creation_idempotency_key=key, registry=registry, policy=policy),
            )["snapshot"]

    seal("initial-catalog")
    frozen = manager.frozen_skill_catalog()
    tool = create_skill_load_tool(
        manager, control_admission=lambda _name, _mode: nullcontext(),
        activation_snapshot_resolver=lambda: frozen,
    )
    first = json.loads(await tool.ainvoke({"name": "shared", "mode": "tracked"}))
    assert first["status"] == "loaded" and first["tracked"] is True
    before = seal("tracked-body")
    old_bytes = canonical_json_bytes(before.to_dict())
    first_frames = before.seal_binding.prefix.state.frames
    same = json.loads(await tool.ainvoke({"name": "shared"}))
    assert (same["tracked"], same["append_status"], same["revision"]) == (False, "already_active", first["revision"])
    assert manager.prepare_pending() is None
    assert seal("snapshot-same").seal_binding.prefix.state.frames == first_frames

    catalog_id = "skill-entry:workspace:catalog:catalog"
    old_catalog = registry.snapshot(catalog_id).revision
    _write_shared_skill(files.workspace_root / ".boxteam" / "skills", "显式刷新来源", WORKSPACE_BODY)
    async with asyncio.timeout(5):
        while registry.snapshot(catalog_id).revision == old_catalog:
            await asyncio.sleep(0.01)
    seal("new-catalog")
    frozen = manager.frozen_skill_catalog()
    refreshed = json.loads(await tool.ainvoke({"name": "shared"}))
    assert refreshed["status"] == "loaded" and refreshed["tracked"] is False
    assert refreshed["revision"] == frozen.entries["shared"].activation_revision != first["revision"]
    updated = seal("refreshed-body")
    frames = updated.seal_binding.prefix.state.frames
    assert frames[:len(first_frames)] == first_frames
    new_source = "skill:" + frozen.entries["shared"].entry_identity
    (new_frame, new_ref, new_block), = [(frame, ref, block) for frame in frames if frame.role == "user" and frame.source_protocol is not None
        for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
        if ref.source_identity == new_source]
    assert new_block.body == WORKSPACE_BODY
    switched, = [block for block in json.loads(new_frame.content)["sources"] if block["source_identity"] == new_source]
    assert (switched["source_identity"], switched["revision"], switched["operation"]) == (
        new_source, refreshed["revision"], "replace",
    )
    previous = next(ref for frame in first_frames for ref in frame.item_refs if ref.source_identity is not None
                    and ref.source_identity.startswith("skill:"))
    assert switched["replaces"] == [{
        "source_identity": previous.source_identity,
        "revision": first["revision"],
    }]
    assert new_ref.source_tracking_at_seal == "untracked"
    enabled = json.loads(await tool.ainvoke({"name": "shared", "mode": "tracked"}))
    assert enabled["tracked"] is True and enabled["append_status"] == "already_active"
    assert manager.prepare_pending() is None
    controls = {state.source_id: state for state in saver.load_context_source_control_states(owner)}
    assert controls[new_source].tracking_status == "tracked"
    assert controls[new_source].latest_visible_committed_revision == refreshed["revision"]
    assert canonical_json_bytes(before.to_dict()) == old_bytes
    assert json.loads(await tool.ainvoke({"name": "shared", "mode": "untrack"}))["tracked"] is False
    old_catalog = registry.snapshot(catalog_id).revision
    (files.workspace_root / ".boxteam" / "skills" / "shared" / "SKILL.md").unlink()
    async with asyncio.timeout(5):
        while registry.snapshot(catalog_id).revision == old_catalog:
            await asyncio.sleep(0.01)
    seal("gateway-catalog-again")
    frozen = manager.frozen_skill_catalog()
    reselected = json.loads(await tool.ainvoke({"name": "shared"}))
    assert reselected["revision"] == first["revision"]
    assert reselected["queued"] is True and reselected["append_status"] == "appended"
    gateway_source = previous.source_identity
    assert isinstance(gateway_source, str)
    pending = {state.source_id: state for state in saver.load_context_source_control_states(owner)}[gateway_source]
    assert pending.selection_sequence > pending.latest_visible_selection_sequence
    # 接受后丢弃驻留 CSM；下一次 preparation 必须从原 owner 控制行和冻结版本恢复。
    manager = ContextSourceManager(owner=owner, control_state_port=saver)
    returned = seal("gateway-body-again")
    returned_block = json.loads(returned.seal_binding.prefix.state.frames[-1].content)["sources"][0]
    assert (returned_block["source_identity"], returned_block["revision"], returned_block["operation"]) == (
        gateway_source, first["revision"], "replace",
    )
    assert returned_block["replaces"] == [{"source_identity": new_source, "revision": refreshed["revision"]}]
    assert returned.seal_binding.prefix.state.frames[:-1] == seal("gateway-catalog-again").seal_binding.prefix.state.frames
    selected = {state.source_id: state for state in saver.load_context_source_control_states(owner)}[gateway_source]
    assert selected.selection_sequence == selected.latest_visible_selection_sequence == pending.selection_sequence
    tool = create_skill_load_tool(manager, control_admission=lambda _name, _mode: nullcontext(),
                                  activation_snapshot_resolver=lambda: frozen)
    assert json.loads(await tool.ainvoke({"name": "shared"}))["append_status"] == "already_active"
    assert manager.prepare_pending() is None
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for sealed in (before, updated, returned):
            restored = cold.get_context_assembly(session, thread, assembly_id=sealed.assembly_id)
            assert canonical_json_bytes(restored.to_dict()) == canonical_json_bytes(sealed.to_dict())
            assert restored.request_hash == sealed.request_hash
            projected = cold.project_context_plan_for_dispatch(
                session, thread, restored.as_sealed_plan(), target_format="chat_completions",
            )
            if sealed is updated:
                content = projected.messages[-1].content
                assert isinstance(content, str)
                assert json.loads(content)["sources"][0]["replaces"] == switched["replaces"]
        assert cold.load_context_source_control_states(owner) == saver.load_context_source_control_states(owner)
