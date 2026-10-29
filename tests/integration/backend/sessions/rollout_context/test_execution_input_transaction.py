"""内部输入提交前后中断时，准入、队列与 canonical 正文仍共享同一边界。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest
from langchain_core.messages import HumanMessage

from app.domain.itemized.execution_admission import InternalExecutionIntent
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.prompting.factory import PromptSection, internal_message_factory
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.rollout_context.test_schema4_crash_reader import (
    ReaderCase,
)
from tests.integration.backend.sessions.rollout_context.test_schema4_crash_reader import (
    reader_case as reader_case,  # noqa: PLC0414
)


@pytest.fixture
def internal_input() -> tuple[InternalExecutionIntent, HumanMessage, ExecutionQueueInput]:
    intent = InternalExecutionIntent(
        message_id="message-interrupted-input",
        execution_id="execution-interrupted-input",
        admission_idempotency_key="admission-interrupted-input",
    )
    notice = internal_message_factory.build(
        kind="generated_session_result",
        control="消费本次已完成的结果。",
        sections=(
            PromptSection("control_context", {"source": "transaction-test"}),
            PromptSection("generated_session_result", "保留已提交结果 Ω"),
        ),
        display_content="结果已就绪。",
    )
    message = HumanMessage(
        id=intent.message_id,
        content=notice.content,
        response_metadata={
            "message_id": intent.message_id,
            "created_at": "2026-10-29T00:00:00+00:00",
            "message_metadata": notice.metadata,
        },
    )
    queue_input = ExecutionQueueInput(
        execution_binding=intent, message_id=intent.message_id,
        agent_id="default", provider_id=None, delivery_policy="after_turn",
        gateway_id=None, request_id=None, created_at="2026-10-29T00:00:00+00:00",
    )
    return intent, message, queue_input


def _durable_rows(case: ReaderCase) -> dict[str, list[tuple[object, ...]]]:
    with closing(sqlite3.connect(case.index)) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("database_meta", "storage_commits", "item_catalog", "executions", "execution_queue")
        }


@pytest.mark.parametrize("committed", [False, True], ids=["before_commit", "after_commit"])
@pytest.mark.parametrize("failure_type", [OSError, asyncio.CancelledError], ids=["io_failure", "cancelled"])
def test_interrupted_internal_input_preserves_commit_boundary_and_retries_once(
    reader_case: ReaderCase,
    internal_input: tuple[InternalExecutionIntent, HumanMessage, ExecutionQueueInput],
    committed: bool,
    failure_type: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = reader_case
    intent, message, queue_input = internal_input
    storage = case.saver._storage.storage
    canonical = storage.jsonl_path(case.session, thread_id=case.thread)
    original = canonical.read_bytes()
    rows_before = _durable_rows(case)
    commit = storage._commit_connection
    failure = failure_type("内部输入在提交边界中断")
    commit_calls: list[bool] = []

    def interrupted_commit(connection: sqlite3.Connection) -> None:
        assert connection.in_transaction
        if committed:
            commit(connection)
        commit_calls.append(connection.in_transaction)
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_commit_connection", interrupted_commit)
        with pytest.raises(failure_type) as caught:
            case.saver.admit_internal_execution(
                case.session, thread_id=case.thread, job_id="job-interrupted-input",
                intent=intent, message=message, queue_input=queue_input,
            )
    assert caught.value is failure
    assert commit_calls == [not committed]
    after_failure = canonical.read_bytes()
    rows_after = _durable_rows(case)
    if committed:
        assert after_failure.startswith(original)
        assert len(after_failure.splitlines()) == len(original.splitlines()) + 1
        assert len(rows_after["item_catalog"]) == len(rows_before["item_catalog"]) + 1
        assert len(rows_after["storage_commits"]) == len(rows_before["storage_commits"]) + 1
        assert len(rows_after["executions"]) == len(rows_before["executions"]) + 1
        assert len(rows_after["execution_queue"]) == 1
    else:
        assert after_failure == original
        assert rows_after == rows_before

    with RolloutCheckpointSaver(sessions_dir=storage.sessions_dir) as cold:
        admitted = cold.admit_internal_execution(
            case.session, thread_id=case.thread, job_id="job-interrupted-input",
            intent=intent, message=message, queue_input=queue_input,
        )
        assert (admitted.session_id, admitted.thread_id, admitted.execution_id, admitted.turn_id) == (
            case.session, case.thread, intent.execution_id, None,
        )
        assert cold.accepted_internal_message(
            case.session, thread_id=case.thread, job_id=admitted.job_id,
            message_id=intent.message_id, intent=intent,
        ).content == message.content
        record, = cold.execution_queue_snapshot(case.session, case.thread).records
        assert record.input == queue_input
        assert record.cursor.admission == admitted
        assert record.phase == "queued"
        items = cold._storage.storage.read_items(case.session, case.thread)
        assert items[-1].item_id == intent.notice_item_id
        assert items[-1].turn_id is None
        with cold._storage.storage.read_connection(case.session, case.thread) as connection:
            assert connection.execute(
                "SELECT execution_id FROM item_catalog WHERE item_id=?", (intent.notice_item_id,),
            ).fetchone() == (intent.execution_id,)
        saved_bytes, saved_rows = canonical.read_bytes(), _durable_rows(case)
        if committed:
            assert saved_bytes == after_failure
            assert saved_rows == rows_after
        assert cold.admit_internal_execution(
            case.session, thread_id=case.thread, job_id=admitted.job_id,
            intent=intent, message=message, queue_input=queue_input,
        ) == admitted
        assert canonical.read_bytes() == saved_bytes
        assert _durable_rows(case) == saved_rows
