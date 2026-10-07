from __future__ import annotations

from app.domain.itemized.mutation_intents import ApplySourceLifecycleDecision
from app.services.business.system_reminder_checkpoint_service import (
    persist_interrupt_checkpoint,
)

THREAD_ID = "thr_018f1234567870008000000000000001"


class FakeMutationIntentOwner:
    def __init__(self) -> None:
        self.intents: list[ApplySourceLifecycleDecision] = []

    def get_tuple(self, _config: dict) -> None:
        raise AssertionError("reminder 不得读取 LangGraph checkpoint")

    def put(self, **_kwargs: object) -> None:
        raise AssertionError("reminder 不得直接写入 LangGraph checkpoint")

    def submit_execution_pending_source(self, intent: object) -> None:
        if not isinstance(intent, ApplySourceLifecycleDecision):
            raise TypeError(f"收到错误的 mutation intent: {intent!r}")
        self.intents.append(intent)


def test_persist_interrupt_checkpoint_submits_pending_source_intent() -> None:
    owner = FakeMutationIntentOwner()

    persist_interrupt_checkpoint(
        checkpointer=owner,
        session_id="sess_interrupt",
        thread_id=THREAD_ID,
        active_tool_name=None,
        event_identity="turn-interrupt", execution_id="execution-fixture",
    )

    assert len(owner.intents) == 1
    decision = owner.intents[0]
    assert decision.source_kind == "checkpoint_reminder"
    assert decision.source_id == "checkpoint:interrupt:turn-interrupt"
    assert decision.decision_kind == "observe_pending"
    assert decision.pending_only is True
    assert decision.turn_scope == "pending_next_turn"
    assert decision.item_id is not None
    assert decision.content is not None
    assert "<system_reminder>" in decision.content
    assert "文本生成" in decision.content
    assert decision.extensions["boxteam.checkpoint.annotations"]["value"]["source"] == "interrupt"
    assert decision.extensions["boxteam.checkpoint.annotations"]["value"]["internal"] is True


def test_persist_interrupt_checkpoint_submits_tool_reminder_without_checkpoint_mutation() -> (
    None
):
    owner = FakeMutationIntentOwner()

    persist_interrupt_checkpoint(
        checkpointer=owner,
        session_id="sess_interrupt",
        thread_id=THREAD_ID,
        active_tool_name="python_exec",
        event_identity="turn-tool-interrupt", execution_id="execution-fixture",
    )

    assert len(owner.intents) == 1
    decision = owner.intents[0]
    assert decision.source_id == "checkpoint:interrupt:turn-tool-interrupt"
    assert decision.content is not None
    assert "<system_reminder>" in decision.content
    assert "python_exec" in decision.content
    assert decision.extensions["boxteam.checkpoint.annotations"]["value"]["phase"] == "tool"
    assert decision.extensions["boxteam.checkpoint.annotations"]["value"]["tool_name"] == "python_exec"
