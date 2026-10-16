"""固定 user admission 不得被 internal HumanMessage 改写归属。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord
from app.prompting.factory import PromptSection, internal_message_factory
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.message_commit.prepare import (
    prepare_messages,
)

SESSION_ID = "session-admission-conflict"
THREAD_ID = "thread-admission-conflict"


def _unexpected_read_item(*args: object, **kwargs: object) -> CanonicalItemRecord:
    raise AssertionError("新消息不能读取既有 canonical item")


def test_user_turn_admission_rejects_internal_human_message(tmp_path: Path) -> None:
    prompt = internal_message_factory.build(
        kind="generated_session_result",
        control="汇报已完成的内部子任务。",
        sections=(
            PromptSection("control_context", {"generated_session_id": "child-session"}),
            PromptSection("generated_session_result", "子任务结果正文。"),
        ),
        display_content="内部子任务已完成。",
    )
    message = HumanMessage(
        content=prompt.content,
        id="internal-notice-under-user-admission",
        response_metadata=prompt.metadata,
    )
    admission = ExecutionAdmission(
        session_id=SESSION_ID,
        thread_id=THREAD_ID,
        execution_id="execution-user-conflict",
        job_id="job-user-conflict",
        admission_idempotency_key="admission-user-conflict",
        execution_origin="user_turn",
        turn_id="turn-user-conflict",
    )
    owner = MutationIntentOwner(session_id=SESSION_ID, thread_id=THREAD_ID)
    jsonl = tmp_path / "messages.jsonl"
    jsonl.write_bytes(b"")

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(
            "CREATE TABLE messages ("
            "message_id TEXT, message_sequence INTEGER, turn_id TEXT, execution_id TEXT, "
            "jsonl_offset INTEGER, jsonl_length INTEGER);"
            "CREATE TABLE item_catalog ("
            "item_id TEXT, jsonl_offset INTEGER, jsonl_length INTEGER, "
            "turn_id TEXT, execution_id TEXT);"
        )
        with (
            session_thread_owner_scope(owner, langgraph_thread_id=THREAD_ID),
            execution_admission_scope(admission, langgraph_thread_id=THREAD_ID),
            pytest.raises(
                RuntimeError,
                match="user_turn execution admission 不得写入 internal HumanMessage",
            ),
        ):
            prepare_messages(
                connection=connection,
                jsonl=jsonl,
                codec=LangChainMessageCodec(),
                read_item=_unexpected_read_item,
                thread_id=THREAD_ID,
                messages=[message],
                last_message_sequence=0,
                next_item_sequence=1,
                offset=0,
            )

        assert connection.execute("SELECT * FROM messages").fetchall() == []
        assert connection.execute("SELECT * FROM item_catalog").fetchall() == []
    assert jsonl.read_bytes() == b""
