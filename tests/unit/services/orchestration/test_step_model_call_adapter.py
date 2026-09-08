"""Step 终态只引用 Saver 已提交的 final item，不生成第二份正文。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from app.domain.itemized.records import CanonicalItemRecord
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter


@pytest.fixture
def final_item() -> CanonicalItemRecord:
    return CanonicalItemRecord.create(
        item_sequence=2,
        item_id="item-msg_final",
        semantic_kind="assistant_output",
        payload_kind="text",
        status="completed",
        producer_ref={"producer_kind": "provider", "producer_id": "model_1"},
        payload="最终回答",
        turn_id="turn_1",
        turn_scope="turn_member",
    )


@pytest.fixture
def saver(final_item: CanonicalItemRecord) -> MagicMock:
    port = MagicMock(
        spec=[
            "get_canonical_item", "execution_for_turn", "converge_execution", "append_items",
            "register_model_call", "consume_prepared_context_for_dispatch",
            "update_model_call_outcome",
        ]
    )
    port.get_canonical_item.return_value = final_item
    port.execution_for_turn.return_value = "execution_1"
    port.consume_prepared_context_for_dispatch.return_value = {
        "assembly_id": "assembly_1", "execution_id": "execution_1",
    }
    return port


@pytest.fixture
def adapter(saver: MagicMock) -> StepModelCallAdapter:
    return StepModelCallAdapter(
        checkpointer=saver,
        session_id="session_1",
        turn_id="turn_1",
        checkpoint_ns="agent",
    )


@pytest.mark.asyncio
async def test_finalization_reuses_committed_item(
    adapter: StepModelCallAdapter, saver: MagicMock
) -> None:
    await adapter.converge_final_checkpoint("msg_final")

    saver.get_canonical_item.assert_called_once_with(
        "session_1", item_id="item-msg_final", checkpoint_ns="agent"
    )
    saver.converge_execution.assert_called_once_with(
        "session_1",
        turn_id="turn_1",
        execution_id="execution_1",
        outcome="completed",
        turn_status="completed",
        final_item_id="item-msg_final",
        checkpoint_ns="agent",
    )
    saver.append_items.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "partial", "wrong_turn", "wrong_id", "reasoning"])
async def test_invalid_final_item_cannot_be_recreated_or_finalized(
    adapter: StepModelCallAdapter,
    saver: MagicMock,
    final_item: CanonicalItemRecord,
    invalid: str,
) -> None:
    replacements = {
        "partial": {"status": "partial"},
        "wrong_turn": {"turn_id": "another_turn"},
        "wrong_id": {"item_id": "item-another_message"},
        "reasoning": {"semantic_kind": "reasoning"},
    }
    saver.get_canonical_item.return_value = (
        None if invalid == "missing" else replace(final_item, **replacements[invalid])
    )

    with pytest.raises(RuntimeError, match="同一 Turn 已提交的 assistant item"):
        await adapter.converge_final_checkpoint("msg_final")

    saver.converge_execution.assert_not_called()
    saver.execution_for_turn.assert_not_called()
    saver.append_items.assert_not_called()


@pytest.mark.asyncio
async def test_empty_output_only_converges_control_state(
    adapter: StepModelCallAdapter, saver: MagicMock
) -> None:
    await adapter.converge_final_checkpoint(None)

    saver.get_canonical_item.assert_not_called()
    saver.append_items.assert_not_called()
    saver.converge_execution.assert_called_once_with(
        "session_1",
        turn_id="turn_1",
        execution_id="execution_1",
        outcome="completed_empty",
        turn_status="completed_empty",
        final_item_id=None,
        checkpoint_ns="agent",
    )


@pytest.mark.parametrize("name", [
    "register_model_call", "consume_prepared_context_for_dispatch",
    "update_model_call_outcome", "execution_for_turn", "get_canonical_item",
    "converge_execution",
])
@pytest.mark.parametrize("missing", [True, False])
def test_missing_owner_port_fails_before_dispatch(
    saver: MagicMock, name: str, missing: bool,
) -> None:
    if missing:
        delattr(saver, name)
    else:
        setattr(saver, name, "不可调用的端口")
    with pytest.raises(TypeError, match=f"缺少可调用端口: {name}"):
        StepModelCallAdapter(
            checkpointer=saver, session_id="session_1", turn_id="turn_1", checkpoint_ns=""
        )
    assert saver.mock_calls == []


@pytest.mark.asyncio
async def test_registration_only_binds_prepared_assembly(
    adapter: StepModelCallAdapter, saver: MagicMock,
) -> None:
    await adapter.register("model_1", 1, "provider_1")
    saver.register_model_call.assert_called_once_with(
        "session_1", execution_id="execution_1", model_call_id="model_1", attempt=1,
        provider="provider_1", retry_of_model_call_id=None, assembly_id="assembly_1",
        dispatch_state="dispatched", checkpoint_ns="agent",
    )
    saver.consume_prepared_context_for_dispatch.assert_called_once_with(
        "session_1", turn_id="turn_1", checkpoint_ns="agent",
    )
    saver.execution_for_turn.assert_not_called()
    saver.append_items.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", [
    None, {}, {"assembly_id": "assembly_1"},
    {"assembly_id": "", "execution_id": "execution_1"},
    {"assembly_id": "assembly_1", "execution_id": " "},
    {"assembly_id": 1, "execution_id": "execution_1"},
])
async def test_unsealed_dispatch_cannot_be_rebuilt(
    adapter: StepModelCallAdapter, saver: MagicMock, prepared: object,
) -> None:
    saver.consume_prepared_context_for_dispatch.return_value = prepared
    with pytest.raises(RuntimeError, match="prepared context|assembly_id/execution_id"):
        await adapter.register("model_1", 1, "provider_1")
    saver.register_model_call.assert_not_called()
    saver.execution_for_turn.assert_not_called()
    saver.append_items.assert_not_called()


@pytest.mark.asyncio
async def test_prepared_handle_requires_object(
    adapter: StepModelCallAdapter, saver: MagicMock,
) -> None:
    saver.consume_prepared_context_for_dispatch.return_value = []
    with pytest.raises(TypeError, match="handle 必须是 object"):
        await adapter.register("model_1", 1, "provider_1")
    saver.register_model_call.assert_not_called()


@pytest.mark.asyncio
async def test_failed_registration_does_not_advance_retry_lineage(
    adapter: StepModelCallAdapter, saver: MagicMock,
) -> None:
    saver.register_model_call.side_effect = [None, RuntimeError("提交失败"), None]
    await adapter.register("model_1", 1, "provider_1")
    with pytest.raises(RuntimeError, match="提交失败"):
        await adapter.register("model_2", 2, "provider_1")
    await adapter.register("model_3", 3, "provider_1")
    assert [
        call.kwargs["retry_of_model_call_id"]
        for call in saver.register_model_call.call_args_list
    ] == [None, "model_1", "model_1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, dispatch_state", [
    ("completed", "completed"), ("completed_empty", "completed"),
    ("failed", "failed"), ("cancelled", "failed"), ("unknown", "unknown"),
])
async def test_outcome_uses_required_owner_port(
    adapter: StepModelCallAdapter, saver: MagicMock, outcome: str, dispatch_state: str,
) -> None:
    await adapter.update_outcome("model_1", outcome)
    saver.update_model_call_outcome.assert_called_once_with(
        "session_1", model_call_id="model_1", outcome=outcome,
        dispatch_state=dispatch_state, checkpoint_ns="agent",
    )


@pytest.mark.asyncio
async def test_outcome_commit_error_is_not_swallowed(
    adapter: StepModelCallAdapter, saver: MagicMock,
) -> None:
    saver.update_model_call_outcome.side_effect = RuntimeError("终态提交失败")
    with pytest.raises(RuntimeError, match="终态提交失败"):
        await adapter.update_outcome("model_1", "completed")
