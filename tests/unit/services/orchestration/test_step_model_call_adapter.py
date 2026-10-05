"""Step 终态只引用 Saver 已提交的 final item，不生成第二份正文。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter


@pytest.fixture(autouse=True)
def admission_context():
    admission = ExecutionAdmission(
        session_id="session_1",
        thread_id="thread_1",
        execution_id="execution_1",
        job_id="job_1",
        admission_idempotency_key="admission_1",
        execution_origin="user_turn",
        turn_id="turn_1",
    )
    owner = MutationIntentOwner(session_id="session_1", thread_id="thread_1")
    with (
        session_thread_owner_scope(owner, langgraph_thread_id="thread_1"),
        execution_admission_scope(admission, langgraph_thread_id="thread_1"),
    ):
        yield admission


@pytest.fixture
def saver() -> MagicMock:
    port = MagicMock(
        spec=[
            "converge_execution", "append_items",
            "register_model_call", "consume_prepared_context_for_dispatch",
            "update_model_call_outcome",
        ]
    )
    port.consume_prepared_context_for_dispatch.return_value = {
        "assembly_id": "assembly_1", "execution_id": "execution_1",
    }
    return port


@pytest.fixture
def adapter(saver: MagicMock) -> StepModelCallAdapter:
    return StepModelCallAdapter(
        checkpointer=saver,
        session_id="session_1",
        thread_id="thread_1",
        job_id="job_1",
        checkpoint_ns="agent",
    )


@pytest.mark.asyncio
async def test_final_message_converges_atomically_before_checkpoint(
    adapter: StepModelCallAdapter, saver: MagicMock
) -> None:
    adapter.converge_final_message(AIMessage(id="msg_final", content="最终回答"))
    final_item = saver.converge_execution.call_args.kwargs["items"][0]
    assert final_item.item_id == "item-msg_final"
    assert final_item.semantic_kind == "assistant_output"
    assert final_item.status == "completed"
    assert final_item.turn_id == "turn_1"
    assert final_item.turn_scope == "turn_member"
    assert final_item.producer_ref["invocation_id"] == "execution_1"
    saver.converge_execution.assert_called_once_with(
        "session_1",
        turn_id="turn_1",
        thread_id="thread_1",
        execution_id="execution_1",
        outcome="completed",
        turn_status="completed",
        items=(final_item,),
        final_item_id="item-msg_final",
        assembly_id=None,
        checkpoint_ns="agent",
    )
    await adapter.converge_final_checkpoint("msg_final")
    saver.converge_execution.assert_called_once()


@pytest.mark.asyncio
async def test_final_checkpoint_requires_pre_persist_atomic_convergence(
    adapter: StepModelCallAdapter,
    saver: MagicMock,
) -> None:
    with pytest.raises(RuntimeError, match="checkpoint 写入前与 execution 原子收敛"):
        await adapter.converge_final_checkpoint("msg_final")

    saver.converge_execution.assert_not_called()
    saver.append_items.assert_not_called()


@pytest.mark.asyncio
async def test_empty_output_only_converges_control_state(
    adapter: StepModelCallAdapter, saver: MagicMock
) -> None:
    await adapter.converge_final_checkpoint(None)

    saver.append_items.assert_not_called()
    saver.converge_execution.assert_called_once_with(
        "session_1",
        turn_id="turn_1",
        thread_id="thread_1",
        execution_id="execution_1",
        outcome="completed_empty",
        turn_status="completed_empty",
        final_item_id=None,
        checkpoint_ns="agent",
    )


@pytest.mark.parametrize("name", [
    "register_model_call", "consume_prepared_context_for_dispatch",
    "update_model_call_outcome", "converge_execution",
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
            checkpointer=saver,
            session_id="session_1",
            thread_id="thread_1",
            job_id="job_1",
            checkpoint_ns="",
        )
    assert saver.mock_calls == []


@pytest.mark.asyncio
async def test_registration_only_binds_prepared_assembly(
    adapter: StepModelCallAdapter, saver: MagicMock,
) -> None:
    await adapter.register("model_1", 1, "provider_1")
    saver.register_model_call.assert_called_once_with(
        "session_1", thread_id="thread_1", execution_id="execution_1", model_call_id="model_1", attempt=1,
        provider="provider_1", retry_of_model_call_id=None, assembly_id="assembly_1",
        dispatch_state="dispatched", checkpoint_ns="agent",
    )
    saver.consume_prepared_context_for_dispatch.assert_called_once_with(
        "session_1",
        "thread_1",
        execution_id="execution_1",
        checkpoint_ns="agent",
    )
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
async def test_failed_provider_attempt_reuses_sealed_binding_for_fallback(
    adapter: StepModelCallAdapter, saver: MagicMock,
) -> None:
    await adapter.register("model_1", 1, "provider_primary")
    await adapter.update_outcome("model_1", "failed")
    saver.consume_prepared_context_for_dispatch.return_value = None

    await adapter.register("model_2", 2, "provider_fallback")

    second_registration = saver.register_model_call.call_args_list[1]
    assert second_registration.kwargs == {
        "execution_id": "execution_1",
        "thread_id": "thread_1",
        "model_call_id": "model_2",
        "attempt": 2,
        "provider": "provider_fallback",
        "retry_of_model_call_id": "model_1",
        "assembly_id": "assembly_1",
        "dispatch_state": "dispatched",
        "checkpoint_ns": "agent",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "completed_empty", "cancelled", "interrupted", "unknown"])
async def test_non_failed_outcome_cannot_reuse_previous_dispatch_binding(
    adapter: StepModelCallAdapter, saver: MagicMock, outcome: str,
) -> None:
    await adapter.register("model_1", 1, "provider_primary")
    await adapter.update_outcome("model_1", outcome)
    saver.consume_prepared_context_for_dispatch.return_value = None

    with pytest.raises(RuntimeError, match="prepared context"):
        await adapter.register("model_2", 2, "provider_fallback")

    assert saver.register_model_call.call_count == 1


@pytest.mark.asyncio
async def test_failed_outcome_for_stale_call_cannot_open_retry_binding(
    adapter: StepModelCallAdapter, saver: MagicMock,
) -> None:
    await adapter.register("model_1", 1, "provider_primary")
    await adapter.update_outcome("another_model_call", "failed")
    saver.consume_prepared_context_for_dispatch.return_value = None

    with pytest.raises(RuntimeError, match="prepared context"):
        await adapter.register("model_2", 2, "provider_fallback")

    assert saver.register_model_call.call_count == 1


@pytest.mark.asyncio
async def test_failed_outcome_commit_must_succeed_before_retry_binding_opens(
    adapter: StepModelCallAdapter, saver: MagicMock,
) -> None:
    await adapter.register("model_1", 1, "provider_primary")
    saver.update_model_call_outcome.side_effect = RuntimeError("终态提交失败")

    with pytest.raises(RuntimeError, match="终态提交失败"):
        await adapter.update_outcome("model_1", "failed")

    saver.update_model_call_outcome.side_effect = None
    saver.consume_prepared_context_for_dispatch.return_value = None
    with pytest.raises(RuntimeError, match="prepared context"):
        await adapter.register("model_2", 2, "provider_fallback")


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
        thread_id="thread_1", dispatch_state=dispatch_state, checkpoint_ns="agent",
    )


@pytest.mark.asyncio
async def test_outcome_commit_error_is_not_swallowed(
    adapter: StepModelCallAdapter, saver: MagicMock,
) -> None:
    saver.update_model_call_outcome.side_effect = RuntimeError("终态提交失败")
    with pytest.raises(RuntimeError, match="终态提交失败"):
        await adapter.update_outcome("model_1", "completed")


@pytest.mark.asyncio
async def test_internal_final_item_and_terminal_convergence_remain_turnless(
    adapter: StepModelCallAdapter,
    saver: MagicMock,
) -> None:
    admission = ExecutionAdmission(
        session_id="session_1",
        thread_id="thread_1",
        execution_id="execution_1",
        job_id="job_1",
        admission_idempotency_key="admission_internal_1",
        execution_origin="internal",
        turn_id=None,
    )
    with execution_admission_scope(admission, langgraph_thread_id="thread_1"):
        await adapter.register("model_1", 1, "provider_1")
        adapter.converge_final_message(
            AIMessage(id="msg_internal_final", content="内部执行结果")
        )
        await adapter.converge_final_checkpoint("msg_internal_final")

    item = saver.converge_execution.call_args.kwargs["items"][0]
    assert item.item_id == "item-msg_internal_final"
    assert item.turn_id is None
    assert item.turn_scope is None
    assert item.producer_ref["invocation_id"] == "execution_1"
    saver.converge_execution.assert_called_once_with(
        "session_1",
        turn_id=None,
        thread_id="thread_1",
        execution_id="execution_1",
        outcome="completed",
        turn_status=None,
        items=(item,),
        final_item_id=None,
        assembly_id="assembly_1",
        checkpoint_ns="agent",
    )
