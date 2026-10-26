"""独立进程只读恢复已封存 context、source control、ToolSet 与 Provider frames。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    _revision,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from tests.harness.python.run_context import TestRunContext
from tests.unit.services.infrastructure.rollout_context.conftest import SourceSealCase

_CHILD_READ = r"""
import json
import sys
from pathlib import Path

from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.core.session_thread_context import session_thread_owner_scope
from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import ContextSourceOwnerKey
from app.services.infrastructure.tool_selection_store import ToolSelectionStore

sessions, session, thread, assembly_id, workspace_id, workspace_root, checkpoint_id = sys.argv[1:]
from tests.support.session_catalog_owner import fixture_catalog_scope

checkpoint_config = build_checkpoint_config(thread, checkpoint_id=checkpoint_id)

with fixture_catalog_scope() as catalog_owner:
    catalog_owner.get_catalog_components(Path(sessions), workspace_id)
    with RolloutCheckpointSaver(Path(sessions)) as saver:
        with session_thread_owner_scope(
            MutationIntentOwner(session, thread), langgraph_thread_id=thread
        ):
            checkpoint_tuple = saver.get_tuple(checkpoint_config)
        if checkpoint_tuple is None:
            raise RuntimeError(f"冷读 checkpoint 缺失: {checkpoint_id}")
        snapshot = saver.get_context_assembly(session, thread, assembly_id=assembly_id)
        states = saver.load_context_source_control_states(ContextSourceOwnerKey(session, thread))
        desired_tools = ToolSelectionStore(
            boxteam_root=Path(workspace_root) / ".boxteam"
        ).snapshot("default")
        frames = snapshot.seal_binding.prefix.state.frames
        print(json.dumps({
            "assembly": snapshot.to_dict(),
            "frame_bytes": [frame.frame_bytes().decode("utf-8") for frame in frames],
            "frame_hashes": [frame.frame_hash() for frame in frames],
            "source_states": [{
                "source_id": state.source_id,
                "tracking_status": state.tracking_status,
                "latest_revision": state.latest_revision,
                "latest_visible_committed_revision": state.latest_visible_committed_revision,
                "state_revision": state.state_revision,
                "binding_revision": state.binding_revision,
            } for state in states],
            "desired_tool_selection_revision": desired_tools.revision,
            "checkpoint": {
                "config": checkpoint_tuple.config,
                "checkpoint": checkpoint_tuple.checkpoint,
                "metadata": checkpoint_tuple.metadata,
                "parent_config": checkpoint_tuple.parent_config,
                "pending_writes": checkpoint_tuple.pending_writes,
            },
        }, ensure_ascii=False, sort_keys=True))
"""


def _skill_descriptor(name: str) -> ContextSourceDescriptor:
    return ContextSourceDescriptor(
        source_id=f"skill:{name}", source_kind="skill", name=name,
        description=f"{name} instructions", internal_locator=f"/.boxteam/skills/{name}/SKILL.md",
    )


def _install_skill_snapshot(manager: ContextSourceManager, bodies: dict[str, str]) -> None:
    manager.install_skill_activation_snapshot(SkillCatalogActivationSnapshot(
        catalog_revision="sha256:process-recovery-catalog",
        entries={name: SkillCatalogBinding(
            name=name, resource_id=f"skill-entry:{name}", entry_identity=f"skill-entry:{name}",
            display_uri=f"boxteam://workspace/test/resources/skills/{name}/SKILL.md",
            activation_revision=_revision(body), body=body,
        ) for name, body in bodies.items()},
    ))


def _directory_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }


def test_independent_process_reads_frozen_owner_state_after_live_sources_and_desired_tools_change(
    request, session_bundle_factory, frozen_provider_wire, frozen_provider_activation,
):
    run = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    sessions = run.workspace_root / ".boxteam" / "sessions"
    session = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session)
    saver = RolloutCheckpointSaver(sessions)
    request.addfinalizer(lambda: saver.__exit__(None, None, None))
    thread = saver._storage.storage.main_thread_id(session)
    accepted = saver.accept_turn(
        session, thread_id=thread, accepted_ingress_id="process-recovery-ingress",
        acceptance_idempotency_key="process-recovery-acceptance", job_id="process-recovery-job",
        admission_idempotency_key="process-recovery-admission", payload="独立进程恢复基准输入",
    )
    owner = ContextSourceOwnerKey(session, thread)
    manager = ContextSourceManager(owner=owner, control_state_port=saver)
    initial_bodies = {"tracked": "初始 tracked source\n", "later-untracked": "冻结的 untracked source\n"}
    for name in initial_bodies:
        manager.register(_skill_descriptor(name))
    manager.activate_skill_content("tracked", initial_bodies["tracked"])
    manager.activate_skill_content("later-untracked", initial_bodies["later-untracked"])
    _install_skill_snapshot(manager, initial_bodies)
    manager.load_skill("tracked", mode="tracked")
    manager.load_skill("later-untracked", mode="tracked")

    selection_store = ToolSelectionStore(boxteam_root=run.workspace_root / ".boxteam")
    selection_store.apply_changes(agent_id="default", changes={"read_file": (True, True)})
    initial_selection = selection_store.snapshot("default")
    tool_set = ToolSetSnapshot(
        selection_revision=initial_selection.revision,
        tools=({"type": "function", "function": {
            "name": "read_file", "description": "读取文件",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                           "required": ["path"], "additionalProperties": False},
        }},),
    )
    helper = SourceSealCase(saver, owner, accepted, frozen_provider_wire, frozen_provider_activation)
    _batch, _binding = helper.seal(manager, tool_set=tool_set)
    (snapshot,) = saver.list_context_assemblies(session, thread)
    sealed = saver.get_context_assembly(session, thread, assembly_id=snapshot.assembly_id)
    sealed_bytes = canonical_json_bytes(sealed.to_dict())
    sealed_frame_bytes = tuple(frame.frame_bytes() for frame in sealed.seal_binding.prefix.state.frames)
    sealed_tools = sealed.tool_snapshot
    assert sealed_tools and sealed.tool_set_refs
    assert sealed.seal_binding.selection_revision == initial_selection.revision
    assert sealed.seal_binding.applied_toolset_revision == tool_set.revision
    assert sealed.tool_set_refs[0].source_revision == tool_set.revision

    checkpoint = empty_checkpoint()
    checkpoint["id"] = f"checkpoint_{create_uuid_hex()}"
    checkpoint["channel_values"] = {
        "assembly_id": sealed.assembly_id,
        "recovery_marker": "checkpoint survives process restart",
    }
    checkpoint["channel_versions"] = {"assembly_id": "1", "recovery_marker": "1"}
    checkpoint["updated_channels"] = ["assembly_id", "recovery_marker"]
    admission = saver.execution_admission(
        session, thread_id=thread, execution_id=str(accepted["initial_execution_id"]),
    )
    with (
        session_thread_owner_scope(
            MutationIntentOwner(session, thread), langgraph_thread_id=thread,
        ),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        checkpoint_config = saver.put(
            build_checkpoint_config(thread), checkpoint,
            {"source": "loop", "step": 1}, {"assembly_id": "1", "recovery_marker": "1"},
        )
        saver.put_writes(checkpoint_config, [("recovery_marker", "pending recovery write")],
                         "process-recovery-task", "checkpoint-node")
        saved_checkpoint = saver.get_tuple(checkpoint_config)
    assert saved_checkpoint is not None
    checkpoint_payload = {
        "config": saved_checkpoint.config,
        "checkpoint": saved_checkpoint.checkpoint,
        "metadata": saved_checkpoint.metadata,
        "parent_config": saved_checkpoint.parent_config,
        "pending_writes": saved_checkpoint.pending_writes,
    }
    checkpoint_bytes = canonical_json_bytes(checkpoint_payload)

    # 正式 CSM API 更新当前来源：一个 source 解除 tracking，另一个形成未封存 pending candidate。
    assert manager.load_skill("later-untracked", mode="untrack").tracked is False
    assert manager.observe("skill:tracked", "父进程中新观察的 source revision\n").status == "accepted"
    pending = manager.prepare_pending()
    assert pending is not None
    assert pending.deltas[0].source_id == "skill:tracked"

    # 权威 desired ToolSelectionStore 在 seal 后改动；sealed ToolSet 必须继续保持旧 revision/schema。
    selection_store.apply_changes(agent_id="default", changes={"read_file": (False, False)})
    changed_selection = selection_store.snapshot("default")
    assert changed_selection.revision != initial_selection.revision
    assert selection_store._path.exists()
    durable_states = {
        state.source_id: state
        for state in saver.load_context_source_control_states(owner)
    }
    assert durable_states["skill:tracked"].latest_revision == pending.deltas[0].revision
    assert durable_states["skill:tracked"].latest_visible_committed_revision != pending.deltas[0].revision

    workspace_id = load_or_create_workspace_id(run.workspace_root)
    argv = [sys.executable, "-c", _CHILD_READ, str(sessions), session, thread,
            sealed.assembly_id, workspace_id, str(run.workspace_root),
            checkpoint["id"]]
    artifact = run.artifacts_dir / "process-recovery-child.json"
    boxteam = run.workspace_root / ".boxteam"
    before_child = _directory_bytes(boxteam)
    child = subprocess.run(argv, cwd=run.project_root, capture_output=True, text=True, timeout=30, check=False)
    artifact.write_text(json.dumps({
        "argv": argv, "returncode": child.returncode, "stdout": child.stdout,
        "stderr": child.stderr, "sealed_request_hash": sealed.request_hash,
        "source_revision_after_observe": pending.deltas[0].revision,
        "desired_tool_selection_revision_after_change": changed_selection.revision,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    assert child.returncode == 0, child.stdout + child.stderr
    restored = json.loads(child.stdout)
    assert canonical_json_bytes(restored["assembly"]) == sealed_bytes
    assert canonical_json_bytes(restored["checkpoint"]) == checkpoint_bytes
    assert tuple(value.encode("utf-8") for value in restored["frame_bytes"]) == sealed_frame_bytes
    assert canonical_json_bytes(restored["assembly"]["tool_snapshot"]) == canonical_json_bytes(sealed_tools)
    restored_states = {state["source_id"]: state for state in restored["source_states"]}
    assert set(restored_states) == set(durable_states)
    for source_id, expected in durable_states.items():
        actual = restored_states[source_id]
        assert actual["latest_revision"] == expected.latest_revision
        assert actual["latest_visible_committed_revision"] == expected.latest_visible_committed_revision
        assert actual["tracking_status"] == expected.tracking_status
        assert actual["state_revision"] == expected.state_revision
        assert actual["binding_revision"] == expected.binding_revision
    assert restored["assembly"]["request_hash"] == sealed.request_hash
    assert restored["desired_tool_selection_revision"] == changed_selection.revision
    assert sealed.seal_binding.selection_revision == initial_selection.revision
    assert sealed.seal_binding.applied_toolset_revision == tool_set.revision
    assert restored_states["skill:tracked"]["latest_revision"] == pending.deltas[0].revision
    assert restored_states["skill:tracked"]["latest_visible_committed_revision"] != pending.deltas[0].revision
    assert _directory_bytes(boxteam) == before_child
    assert saver.get_context_assembly(session, thread, assembly_id=sealed.assembly_id) == sealed
    states = {state.source_id: state for state in saver.load_context_source_control_states(owner)}
    assert states["skill:tracked"].latest_revision == pending.deltas[0].revision
    assert states["skill:tracked"].latest_visible_committed_revision != pending.deltas[0].revision
    assert states["skill:later-untracked"].tracking_status == "untracked"
    assert selection_store.snapshot("default").revision == changed_selection.revision
