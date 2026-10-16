"""真实 seal、admission 与 canonical commit 决定原工具 checkpoint 是否可续接。"""

from contextlib import closing
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
)
from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)


@pytest.fixture
def pending_owner_case(source_seal_case):
    case = source_seal_case
    manager = case.manager()
    manager.register(ContextSourceDescriptor(
        source_id="recovery-skill", source_kind="skill", name="recovery",
        description="只读恢复合同", internal_locator="/.boxteam/skills/recovery/SKILL.md",
    ))
    manager.activate_skill_content("recovery", "沿原工具调用读取冻结上下文。")
    receipt, _ = case.seal(manager)
    owner, saver = case.owner, case.saver
    execution = str(case.accepted["initial_execution_id"])
    admission = saver.execution_admission(owner.session_id, thread_id=owner.thread_id, execution_id=execution)
    assembly = saver.get_context_assembly(owner.session_id, owner.thread_id, assembly_id=receipt.assembly_id)
    saver.register_model_call(
        owner.session_id, thread_id=owner.thread_id, execution_id=execution,
        model_call_id=assembly.model_call_id, attempt=1, provider="recovery-contract",
        assembly_id=assembly.assembly_id, dispatch_state="dispatched",
    )
    assistant = AIMessage(
        id="recovery-carrier", content=[{"type": "reasoning_content", "text": "沿原冻结输入继续。"}],
        tool_calls=[
            {"id": "read", "name": "read_context", "args": {}, "type": "tool_call"},
            {"id": "wait", "name": "wait_for_session", "args": {}, "type": "tool_call"},
        ], response_metadata={"model_call_id": assembly.model_call_id},
    )
    items = tuple(LangChainMessageCodec().items_for_message(
        assistant, item_sequence=1, message_id=assistant.id, turn_id=admission.turn_id,
        execution_id=execution, timestamp="2026-10-09T00:00:00+00:00", model_call_id=assembly.model_call_id,
    ))
    stream = CanonicalBlockAccumulator(
        turn_id=admission.turn_id, execution_id=execution,
        producer_id=assembly.model_call_id, model_call_id=assembly.model_call_id,
    )
    stream.accept({
        "block_id": "recovery-reasoning", "block_index": 0,
        "carrier_type": "reasoning_content", "text": "沿原冻结输入继续。",
    })
    with (
        session_thread_owner_scope(MutationIntentOwner(owner.session_id, owner.thread_id), langgraph_thread_id=owner.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=owner.thread_id),
    ):
        saver.append_items(owner.session_id, stream.finalize(first_item_sequence=1), thread_id=owner.thread_id)
        saver.append_items(owner.session_id, items, thread_id=owner.thread_id)
    return case, admission, assembly, items


def test_unknown_dispatched_call_requires_complete_committed_group(pending_owner_case):
    case, admission, assembly, items = pending_owner_case
    result = case.saver.read_resumable_model_call(admission, assembly.model_call_id, carrier_items=items)
    model, assembly_id = result
    assert model.execution_id == admission.execution_id
    assert model.model_call_id == assembly.model_call_id
    assert assembly_id == assembly.assembly_id
    assert case.saver.read_resumable_model_call(admission, assembly.model_call_id, carrier_items=items[:-1]) is None
    with closing(case.saver._storage.storage._connect(admission.thread_id, session_id=admission.session_id)) as connection:
        connection.execute("UPDATE item_catalog SET content_hash=? WHERE item_id=?", ("sha256:" + "f" * 64, items[0].item_id))
        connection.commit()
    assert case.saver.read_resumable_model_call(admission, assembly.model_call_id, carrier_items=items) is None


@pytest.mark.parametrize("defect", ["partial", "missing_group"])
def test_missing_completion_or_typed_group_is_not_recovered(pending_owner_case, defect):
    case, admission, assembly, items = pending_owner_case
    with closing(case.saver._storage.storage._connect(admission.thread_id, session_id=admission.session_id)) as connection:
        if defect == "partial":
            connection.execute("UPDATE item_catalog SET status='partial' WHERE item_id=?", (items[-1].item_id,))
        else:
            connection.execute("UPDATE item_catalog SET projection_manifest_json=json_set(projection_manifest_json,'$.projection_group',NULL) WHERE item_id=?", (items[-1].item_id,))
        connection.commit()
    assert case.saver.read_resumable_model_call(admission, assembly.model_call_id, carrier_items=items) is None


def test_terminal_execution_never_resumes_even_with_complete_carrier(pending_owner_case):
    case, admission, assembly, items = pending_owner_case
    case.saver.mark_execution_lost(
        admission.session_id, thread_id=admission.thread_id, turn_id=admission.turn_id,
        execution_id=admission.execution_id, reason="recovery-contract-terminal",
    )
    assert case.saver.read_resumable_model_call(admission, assembly.model_call_id, carrier_items=items) is None


def test_original_admission_identity_cannot_be_substituted(pending_owner_case):
    case, admission, assembly, items = pending_owner_case
    with pytest.raises(RuntimeError, match="admission/assembly owner 漂移"):
        case.saver.read_resumable_model_call(replace(admission, job_id="other-job"), assembly.model_call_id, carrier_items=items)


def test_sealed_request_owner_must_match_original_model(pending_owner_case):
    case, admission, assembly, items = pending_owner_case
    with closing(case.saver._storage.storage._connect(admission.thread_id, session_id=admission.session_id)) as connection:
        connection.execute("UPDATE context_assemblies SET model_call_id=? WHERE assembly_id=?", ("foreign-model", assembly.assembly_id))
        connection.commit()
    with pytest.raises(RuntimeError, match="sealed assembly owner 漂移"):
        case.saver.read_resumable_model_call(admission, assembly.model_call_id, carrier_items=items)
