"""真实 Skill 文件发布 → 冻结工具调用 → 模式切换 → seal 与冷读。"""

import asyncio
import json
from contextlib import nullcontext

from app.agents.tools.skill_loading import create_skill_load_tool
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
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
    new_frames = [frame for frame in frames if frame.item_ref.source_identity == new_source]
    assert len(new_frames) == 1 and WORKSPACE_BODY in new_frames[0].content
    assert new_frames[0].item_ref.source_tracking_at_seal == "untracked"
    enabled = json.loads(await tool.ainvoke({"name": "shared", "mode": "tracked"}))
    assert enabled["tracked"] is True and enabled["append_status"] == "already_active"
    assert manager.prepare_pending() is None
    controls = {state.source_id: state for state in saver.load_context_source_control_states(owner)}
    assert controls[new_source].tracking_status == "tracked"
    assert controls[new_source].latest_visible_committed_revision == refreshed["revision"]
    assert canonical_json_bytes(before.to_dict()) == old_bytes
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for sealed in (before, updated):
            restored = cold.get_context_assembly(session, thread, assembly_id=sealed.assembly_id)
            assert canonical_json_bytes(restored.to_dict()) == canonical_json_bytes(sealed.to_dict())
            assert restored.request_hash == sealed.request_hash
        assert cold.load_context_source_control_states(owner) == saver.load_context_source_control_states(owner)
