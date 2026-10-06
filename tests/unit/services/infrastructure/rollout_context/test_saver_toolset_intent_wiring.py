"""5.4 切片：ToolSet desired/applied producer + owner 消费 focused 测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.agents.graph_binding import DEEP_AGENT_GRAPH_BINDING
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_prefixed_id, uuid7_datetime_from_hex
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import (
    MutationIntentOwner,
    SwitchToolSetIntent,
)
from app.services.infrastructure.rollout_context.checkpoint.mutation.mutation_intents import (
    MutationIntentConsumptionRejected,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)

SESSION_ID = "ses_019c38b8b88476a58ce3735618d44bc8"

TOOLS_A = (
    {"type": "function", "function": {"name": "get_goal", "parameters": {}}},
)
TOOLS_B = (
    {"type": "function", "function": {"name": "read_file", "parameters": {}}},
)


def _desired_key(tools: tuple[dict[str, object], ...]) -> str:
    return sha256_jcs({"tools": [dict(tool) for tool in tools]})


@pytest.fixture
def saver(tmp_path: Path, session_bundle_factory) -> RolloutCheckpointSaver:
    session_bundle_factory(tmp_path, SESSION_ID)
    return RolloutCheckpointSaver(sessions_dir=tmp_path)


def _binding_state(saver: RolloutCheckpointSaver):
    control_path, main_thread_id = saver._resolve_main_thread_control(SESSION_ID)
    store = SessionControlStore(control_path)
    try:
        try:
            return store.get_thread_owner_binding(main_thread_id)
        except KeyError:
            return None
    finally:
        store.close()


def _main_thread_id(saver: RolloutCheckpointSaver) -> str:
    return saver._storage._resolve_thread_identity(SESSION_ID, None)


def test_producer_applies_and_increments_only_on_change(
    saver: RolloutCheckpointSaver,
) -> None:
    thread_id = _main_thread_id(saver)
    saver._switch_tool_set_if_needed(SESSION_ID, thread_id, tool_snapshot=TOOLS_A)
    binding = _binding_state(saver)
    assert binding is not None
    assert binding.desired_toolset_revision == 1
    assert binding.applied_toolset_revision == 1
    assert binding.toolset_compatibility_key == _desired_key(TOOLS_A)

    # 同一 desired 不重复消费，也不产生新 revision。
    saver._switch_tool_set_if_needed(SESSION_ID, thread_id, tool_snapshot=TOOLS_A)
    assert _binding_state(saver).applied_toolset_revision == 1

    # desired 变化 → 新 intent → revision 递增。
    saver._switch_tool_set_if_needed(SESSION_ID, thread_id, tool_snapshot=TOOLS_B)
    binding = _binding_state(saver)
    assert binding.applied_toolset_revision == 2
    assert binding.desired_toolset_revision == 2
    assert binding.toolset_compatibility_key == _desired_key(TOOLS_B)


def test_prepare_boundary_switches_and_sealed_retry_keeps_applied(
    frozen_provider_wire, saver: RolloutCheckpointSaver,
) -> None:
    thread_id = _main_thread_id(saver)
    accepted = saver.accept_turn(
        SESSION_ID,
        thread_id=thread_id,
        accepted_ingress_id="toolset-ingress",
        acceptance_idempotency_key="toolset-root",
        job_id="toolset-job",
        admission_idempotency_key="toolset-admission",
        payload="切换工具集",
        payload_kind="text",
    )
    tools_a = ToolSetSnapshot(selection_revision=_desired_key(TOOLS_A), tools=TOOLS_A)
    tools_b = ToolSetSnapshot(selection_revision=_desired_key(TOOLS_B), tools=TOOLS_B)
    options = {
        "turn_id": accepted["turn_id"],
        "execution_id": accepted["initial_execution_id"],
        **frozen_provider_wire("toolset-provider", "responses"),
        "context_source_manager": ContextSourceManager(
            owner=ContextSourceOwnerKey(SESSION_ID, thread_id),
            control_state_port=saver,
        ),
        "tool_set_supplier": lambda: tools_a,
        "plan_creation_idempotency_key": "toolset-create",
        "seal_idempotency_key": "toolset-seal",
    }
    admission = saver.execution_admission(
        SESSION_ID, thread_id=thread_id,
        execution_id=accepted["initial_execution_id"],
    )
    owner = MutationIntentOwner(session_id=SESSION_ID, thread_id=thread_id)
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        first = saver.prepare_context_for_provider(SESSION_ID, thread_id, **options)
        first_snapshot = saver._storage.get_context_assembly(
            SESSION_ID, thread_id, assembly_id=first["assembly_id"],
        )
        binding = first_snapshot.seal_binding
        assert binding.applied_toolset_revision == tools_a.revision
        assert binding.tool_set_snapshot_id == tools_a.snapshot_id
        assert binding.prefix.state.epoch.ordinal == 1

        # 下一次真实 prepare/seal 才使 B 成为 applied，旧 A 请求仍归原 assembly。
        second = saver.prepare_context_for_provider(
            SESSION_ID, thread_id,
            **{**options,
               "tool_set_supplier": lambda: tools_b,
               "plan_creation_idempotency_key": "toolset-create-b",
               "seal_idempotency_key": "toolset-seal-b"},
        )
        second_snapshot = saver._storage.get_context_assembly(
            SESSION_ID, thread_id, assembly_id=second["assembly_id"],
        )
        assert second_snapshot.seal_binding.applied_toolset_revision == tools_b.revision
        assert second_snapshot.seal_binding.tool_set_snapshot_id == tools_b.snapshot_id
        assert second_snapshot.seal_binding.prefix.state.epoch.ordinal == 2
        assert second_snapshot.seal_binding.previous_assembly_id == first["assembly_id"]

        def refuse_supplier():
            pytest.fail("已 sealed 的重试不得重读 ToolSet desired")

        retry = saver.prepare_context_for_provider(
            SESSION_ID, thread_id,
            **{**options, "tool_set_supplier": refuse_supplier},
        )
    assert retry["assembly_id"] == first["assembly_id"]
    assert saver._storage.get_context_assembly(
        SESSION_ID, thread_id, assembly_id=first["assembly_id"],
    ) == first_snapshot
    with saver._context_reader.open_snapshot(SESSION_ID, thread_id) as snapshot:
        latest = saver._storage.latest_context_assembly(snapshot)
    assert latest.assembly_id == second["assembly_id"]
    assert latest.seal_binding.applied_toolset_revision == tools_b.revision
    assert latest.seal_binding.tool_set_snapshot_id == tools_b.snapshot_id


def test_unregistered_thread_cannot_receive_toolset_binding(
    saver: RolloutCheckpointSaver,
) -> None:
    intent = SwitchToolSetIntent(
        owner=MutationIntentOwner(session_id=SESSION_ID, thread_id="child-1"),
        desired_revision="rev-x",
        tool_set_snapshot_id="tool-set:rev-x",
    )
    with pytest.raises(RuntimeError, match="thread_id"):
        saver.consume_mutation_intent(intent)
    assert _binding_state(saver) is None


def test_switch_tool_set_intent_replay_keeps_revision(
    saver: RolloutCheckpointSaver,
) -> None:
    intent = SwitchToolSetIntent(
        owner=MutationIntentOwner(
            session_id=SESSION_ID,
            thread_id=_main_thread_id(saver),
        ),
        desired_revision="rev-1",
        tool_set_snapshot_id="tool-set:rev-1",
    )
    saver.consume_mutation_intent(intent)
    # 跨进程同 identity 重放（直连端口）：applied 历史不可覆盖。
    saver.consume_mutation_intent(intent)
    binding = _binding_state(saver)
    assert binding.applied_toolset_revision == 1
    assert binding.toolset_compatibility_key == "rev-1"


def test_producer_applies_toolset_to_explicit_child_thread(
    saver: RolloutCheckpointSaver,
) -> None:
    child_thread_id = create_prefixed_id("thr")
    session_dir = saver._storage._path_resolver.resolve_session_node(SESSION_ID)
    control = SessionControlStore(session_dir / "session-control.sqlite")
    try:
        control.create_or_get_thread_creation_record(
            idempotency_key=f"toolset-child-{child_thread_id}",
            initial_state="idle",
            preimage_hash="b" * 64,
            graph_binding=json.dumps({
                "graph_id": DEEP_AGENT_GRAPH_BINDING.graph_id,
                "graph_revision": DEEP_AGENT_GRAPH_BINDING.graph_revision,
                "graph_schema_hash": DEEP_AGENT_GRAPH_BINDING.graph_schema_hash,
                "capability_profile_hash": DEEP_AGENT_GRAPH_BINDING.capability_profile_hash,
            }),
            capability_profile="{}",
            created_at=uuid7_datetime_from_hex(child_thread_id[4:]),
            thread_id=child_thread_id,
        )
        artifact_manifest = "{}"
        control.freeze_thread_creation_artifact_manifest(
            f"toolset-child-{child_thread_id}",
            artifact_manifest=artifact_manifest,
            artifact_manifest_hash=hashlib.sha256(
                artifact_manifest.encode("utf-8")
            ).hexdigest(),
        )
        published = control.publish_thread_creation_record(
            f"toolset-child-{child_thread_id}"
        )
        (session_dir / published.final_relative_locator).mkdir(parents=True)
    finally:
        control.close()

    saver._switch_tool_set_if_needed(
        SESSION_ID,
        child_thread_id,
        tool_snapshot=TOOLS_A,
    )

    store = SessionControlStore(session_dir / "session-control.sqlite")
    try:
        binding = store.get_thread_owner_binding(child_thread_id)
    finally:
        store.close()
    assert binding.applied_toolset_revision == 1
    assert binding.toolset_compatibility_key == _desired_key(TOOLS_A)


def test_producer_failure_keeps_zero_half_state_and_retry(
    saver: RolloutCheckpointSaver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*args: object, **kwargs: object) -> None:
        raise OSError("owner 事务失败")

    monkeypatch.setattr(SessionControlStore, "update_thread_owner_binding", _fail)
    with pytest.raises(MutationIntentConsumptionRejected) as exc_info:
        saver._switch_tool_set_if_needed(
            SESSION_ID,
            _main_thread_id(saver),
            tool_snapshot=TOOLS_A,
        )
    assert exc_info.value.code == "mutation-intent-rejected"
    assert exc_info.value.failure_outcome == "keep_applied_toolset"
    monkeypatch.undo()

    # 失败零半状态：owner binding 行可能由 ensure 建立，但 toolset 槽位未推进。
    binding = _binding_state(saver)
    assert binding is None or binding.toolset_compatibility_key is None

    saver._switch_tool_set_if_needed(
        SESSION_ID,
        _main_thread_id(saver),
        tool_snapshot=TOOLS_A,
    )
    binding = _binding_state(saver)
    assert binding.applied_toolset_revision == 1
    assert binding.toolset_compatibility_key == _desired_key(TOOLS_A)
