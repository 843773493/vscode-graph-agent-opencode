"""5.4 切片：ToolSet desired/applied producer + owner 消费 focused 测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.session_control_store import SessionControlStore
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import (
    MutationIntentOwner,
    SwitchToolSetIntent,
)
from app.services.infrastructure.rollout_context.checkpoint.mutation_intents import (
    MutationIntentConsumptionRejected,
    MutationIntentOwnerMismatch,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)

SESSION_ID = "ses_6a1b47e2c05d4f8fa3b7d92c5e041762"

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


def test_producer_applies_and_increments_only_on_change(
    saver: RolloutCheckpointSaver,
) -> None:
    saver._switch_tool_set_if_needed(SESSION_ID, tool_snapshot=TOOLS_A)
    binding = _binding_state(saver)
    assert binding is not None
    assert binding.desired_toolset_revision == 1
    assert binding.applied_toolset_revision == 1
    assert binding.toolset_compatibility_key == _desired_key(TOOLS_A)

    # 同一 desired 不重复消费，也不产生新 revision。
    saver._switch_tool_set_if_needed(SESSION_ID, tool_snapshot=TOOLS_A)
    assert _binding_state(saver).applied_toolset_revision == 1

    # desired 变化 → 新 intent → revision 递增。
    saver._switch_tool_set_if_needed(SESSION_ID, tool_snapshot=TOOLS_B)
    binding = _binding_state(saver)
    assert binding.applied_toolset_revision == 2
    assert binding.desired_toolset_revision == 2
    assert binding.toolset_compatibility_key == _desired_key(TOOLS_B)


def test_prepare_boundary_switches_and_sealed_retry_keeps_applied(
    saver: RolloutCheckpointSaver,
) -> None:
    accepted = saver.accept_turn(
        SESSION_ID,
        accepted_ingress_id="toolset-ingress",
        acceptance_idempotency_key="toolset-root",
        payload="切换工具集",
        payload_kind="text",
    )
    options = {
        "turn_id": accepted["turn_id"],
        "provider_version": "toolset-provider",
        "target_format": "responses",
        "plan_creation_idempotency_key": "toolset-create",
        "seal_idempotency_key": "toolset-seal",
    }
    first = saver.prepare_context_for_provider(
        SESSION_ID, tool_snapshot=TOOLS_A, **options
    )
    binding = _binding_state(saver)
    assert binding.applied_toolset_revision == 1
    assert binding.toolset_compatibility_key == _desired_key(TOOLS_A)

    # 模拟外部 desired 漂移：直连端口把 applied 推进到 B。
    saver.consume_mutation_intent(
        SwitchToolSetIntent(
            owner=MutationIntentOwner(session_id=SESSION_ID, thread_id="main"),
            desired_revision=_desired_key(TOOLS_B),
            tool_set_snapshot_id="tool-set:" + _desired_key(TOOLS_B),
        )
    )
    # 已 sealed 的在飞请求不做二次比较：同 creation key 重试复用 assembly，
    # producer 不被再次触发，applied 不被拉回 A。
    retry = saver.prepare_context_for_provider(
        SESSION_ID, tool_snapshot=TOOLS_A, **options
    )
    assert retry["assembly_id"] == first["assembly_id"]
    binding = _binding_state(saver)
    assert binding.applied_toolset_revision == 2
    assert binding.toolset_compatibility_key == _desired_key(TOOLS_B)


def test_switch_tool_set_intent_owner_mismatch_zero_side_effect(
    saver: RolloutCheckpointSaver,
) -> None:
    intent = SwitchToolSetIntent(
        owner=MutationIntentOwner(session_id=SESSION_ID, thread_id="child-1"),
        desired_revision="rev-x",
        tool_set_snapshot_id="tool-set:rev-x",
    )
    with pytest.raises(MutationIntentOwnerMismatch) as exc_info:
        saver.consume_mutation_intent(intent)
    assert exc_info.value.code == "mutation-intent-owner-mismatch"
    assert _binding_state(saver) is None


def test_switch_tool_set_intent_replay_keeps_revision(
    saver: RolloutCheckpointSaver,
) -> None:
    intent = SwitchToolSetIntent(
        owner=MutationIntentOwner(session_id=SESSION_ID, thread_id="main"),
        desired_revision="rev-1",
        tool_set_snapshot_id="tool-set:rev-1",
    )
    saver.consume_mutation_intent(intent)
    # 跨进程同 identity 重放（直连端口）：applied 历史不可覆盖。
    saver.consume_mutation_intent(intent)
    binding = _binding_state(saver)
    assert binding.applied_toolset_revision == 1
    assert binding.toolset_compatibility_key == "rev-1"


def test_producer_failure_keeps_zero_half_state_and_retry(
    saver: RolloutCheckpointSaver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*args: object, **kwargs: object) -> None:
        raise OSError("owner 事务失败")

    monkeypatch.setattr(SessionControlStore, "update_thread_owner_binding", _fail)
    with pytest.raises(MutationIntentConsumptionRejected) as exc_info:
        saver._switch_tool_set_if_needed(SESSION_ID, tool_snapshot=TOOLS_A)
    assert exc_info.value.code == "mutation-intent-rejected"
    assert exc_info.value.failure_outcome == "keep_applied_toolset"
    monkeypatch.undo()

    # 失败零半状态：owner binding 行可能由 ensure 建立，但 toolset 槽位未推进。
    binding = _binding_state(saver)
    assert binding is None or binding.toolset_compatibility_key is None

    saver._switch_tool_set_if_needed(SESSION_ID, tool_snapshot=TOOLS_A)
    binding = _binding_state(saver)
    assert binding.applied_toolset_revision == 1
    assert binding.toolset_compatibility_key == _desired_key(TOOLS_A)
