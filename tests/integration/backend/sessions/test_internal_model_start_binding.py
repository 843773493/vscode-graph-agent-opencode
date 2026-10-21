"""真实 model-start 缺少目标绑定时拒绝，不能借用已完成的来源 Turn。"""

from __future__ import annotations

from contextlib import nullcontext
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage

from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.prompting.factory import PromptSection, internal_message_factory
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.execution_step.model_call import StepModelCallAdapter
from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    native_dispatch_workspace,
)

__all__ = ["native_dispatch_workspace"]


@pytest.fixture
def internal_binding_case(native_dispatch_workspace, session_bundle_factory):
    sessions = native_dispatch_workspace / ".boxteam/sessions"
    source, target = (f"ses_{create_uuid_hex()}" for _ in range(2))
    for session in (source, target):
        session_bundle_factory(sessions, session)
    with RolloutCheckpointSaver(sessions) as saver:
        source_thread = saver._storage.storage.main_thread_id(source)
        target_thread = saver._storage.storage.main_thread_id(target)
        saver.accept_turn(
            source, thread_id=source_thread, accepted_ingress_id="source-input",
            acceptance_idempotency_key="source-acceptance", job_id="source-job",
            admission_idempotency_key="source-admission", payload="真实来源用户输入",
            turn_id="source-turn", root_item_id="source-root",
        )
        saver.mark_execution_lost(source, thread_id=source_thread, turn_id="source-turn")
        resumed = saver.resume_turn(source, thread_id=source_thread, turn_id="source-turn", job_id="source-resume")
        saver.converge_execution(
            source, thread_id=source_thread, turn_id="source-turn",
            execution_id=resumed["execution_id"],
            outcome="completed_empty", turn_status="completed_empty",
        )
        prepared = internal_message_factory.build(
            kind="generated_session_result", control="生成分支结束",
            sections=(
                PromptSection("control_context", {"source_session_id": source, "source_thread_id": source_thread}),
                PromptSection("generated_session_result", "来源私有结果"),
            ),
            display_content="生成分支已结束，主会话正在处理返回结果。",
        )
        intent = InternalExecutionIntent(
            message_id="target-message", execution_id="target-execution",
            admission_idempotency_key="target-admission",
        )
        admission = saver.admit_internal_execution(
            target, thread_id=target_thread, job_id="target-job", intent=intent,
            message=HumanMessage(
                content=prepared.content, id=intent.message_id,
                response_metadata={
                    "message_id": intent.message_id, "created_at": "2026-10-21T00:00:00+00:00",
                    "message_metadata": prepared.metadata,
                },
            ),
        )
        yield saver, source, source_thread, target, target_thread, admission


def _owner_facts(saver, session, thread):
    with saver._storage.storage.open_read_snapshot(session, thread) as snapshot:
        return {
            table: tuple(snapshot.connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in ("turn_records", "turn_acceptances", "executions", "model_calls", "item_catalog", "storage_commits")
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("target_admitted", [False, True], ids=["missing-admission", "missing-prepared-binding"])
async def test_internal_model_start_without_target_binding_leaves_both_owners_unchanged(
    internal_binding_case, target_admitted,
):
    saver, source, source_thread, target, target_thread, admission = internal_binding_case
    before = {
        source: _owner_facts(saver, source, source_thread),
        target: _owner_facts(saver, target, target_thread),
    }
    assert before[source]["turn_records"]
    assert not before[target]["turn_records"]
    assert admission.turn_id is None and admission.execution_origin == "internal"
    adapter = StepModelCallAdapter(
        checkpointer=saver, session_id=target, thread_id=target_thread,
        job_id=admission.job_id, checkpoint_ns="",
    )
    runtime = MessageStreamRuntime(None, model_call_registrar=adapter.register)
    with (
        session_thread_owner_scope(MutationIntentOwner(target, target_thread), langgraph_thread_id=target_thread),
        execution_admission_scope(admission, langgraph_thread_id=target_thread) if target_admitted else nullcontext(),
    ):
        expected = "prepared context" if target_admitted else "缺少显式 execution admission"
        with pytest.raises(RuntimeError, match=expected):
            await runtime.start_model(str(uuid4()), "provider")
    assert runtime.current_model_call_id is None
    assert runtime.current_attempt == 0
    assert runtime._model_execution_ids == {}
    assert _owner_facts(saver, source, source_thread) == before[source]
    assert _owner_facts(saver, target, target_thread) == before[target]
    assert saver._prepared_dispatches == {}
