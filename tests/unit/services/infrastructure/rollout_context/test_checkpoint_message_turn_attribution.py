"""真实 checkpoint writer 的 Turn/execution attribution 合同。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.prompting.factory import PromptSection, internal_message_factory
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage

SESSION_ID = "ses_019c3635a6c278efb831fe265bdcd656"
OTHER_SESSION_ID = "ses_019c3635a6c278efb831fe265bdcd657"
USER_TURN_ID = "turn-user-attribution"
USER_EXECUTION_ID = "execution-user-attribution"


def _checkpoint(checkpoint_id: str, messages: list[object]) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": checkpoint_id}
    checkpoint["updated_channels"] = ["messages"]
    return checkpoint


def _make_saver(
    tmp_path: Path,
    session_bundle_factory,
    *,
    session_ids: tuple[str, ...] = (SESSION_ID,),
) -> tuple[RolloutStorage, RolloutCheckpointSaver, str]:
    sessions_dir = tmp_path / "sessions"
    for session_id in session_ids:
        session_bundle_factory(sessions_dir, session_id)
    storage = RolloutStorage(
        sessions_dir,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )
    thread_id = get_session_path_resolver(sessions_dir).main_thread_id(SESSION_ID)
    return storage, RolloutCheckpointSaver(sessions_dir, storage=storage), thread_id


def _owner(session_id: str, thread_id: str) -> MutationIntentOwner:
    return MutationIntentOwner(session_id=session_id, thread_id=thread_id)


def _user_admission(
    saver: RolloutCheckpointSaver,
    thread_id: str,
) -> tuple[ExecutionAdmission, HumanMessage]:
    user_message_id = "accepted-user-root"
    user_message = HumanMessage(content="accepted question", id=user_message_id)
    saver.accept_turn(
        SESSION_ID,
        thread_id=thread_id,
        accepted_ingress_id="ingress-message-attribution",
        acceptance_idempotency_key="acceptance-message-attribution",
        job_id="job-message-attribution",
        admission_idempotency_key="admission-message-attribution",
        payload="accepted question",
        payload_kind="text",
        turn_id=USER_TURN_ID,
        root_item_id=f"item-{user_message_id}",
        initial_execution_id=USER_EXECUTION_ID,
    )
    admission = saver.execution_admission(
        SESSION_ID,
        thread_id=thread_id,
        execution_id=USER_EXECUTION_ID,
    )
    return admission, user_message


def _put_with_admission(
    saver: RolloutCheckpointSaver,
    *,
    session_id: str,
    thread_id: str,
    admission: ExecutionAdmission,
    config: RunnableConfig,
    checkpoint_id: str,
    messages: list[object],
) -> RunnableConfig:
    owner = _owner(session_id, thread_id)
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        return saver.put(
            config,
            _checkpoint(checkpoint_id, messages),
            {"source": "unit", "step": checkpoint_id},
            {"messages": checkpoint_id},
        )


def _query(
    storage: RolloutStorage,
    session_id: str,
    thread_id: str,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> list[tuple[object, ...]]:
    with sqlite3.connect(
        storage.index_path(session_id, thread_id=thread_id)
    ) as connection:
        return connection.execute(sql, parameters).fetchall()


def test_new_user_turn_append_uses_acceptance_and_prefix_replay_needs_no_admission(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    storage, saver, thread_id = _make_saver(tmp_path, session_bundle_factory)
    admission, user_message = _user_admission(saver, thread_id)
    assistant_message = AIMessage(content="accepted answer", id="accepted-final")
    initial_config = build_checkpoint_config(thread_id)

    checkpoint_config = _put_with_admission(
        saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        admission=admission,
        config=initial_config,
        checkpoint_id="checkpoint-user-turn",
        messages=[user_message, assistant_message],
    )

    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT message_id, turn_id, execution_id, role FROM messages "
        "ORDER BY message_sequence",
    ) == [
        ("accepted-user-root", USER_TURN_ID, USER_EXECUTION_ID, "user"),
        ("accepted-final", USER_TURN_ID, USER_EXECUTION_ID, "assistant"),
    ]
    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT item_id, turn_id, execution_id FROM item_catalog "
        "WHERE item_id IN (?, ?) ORDER BY item_id",
        ("item-accepted-final", "item-accepted-user-root"),
    ) == [
        ("item-accepted-final", USER_TURN_ID, USER_EXECUTION_ID),
        ("item-accepted-user-root", USER_TURN_ID, USER_EXECUTION_ID),
    ]

    with session_thread_owner_scope(
        _owner(SESSION_ID, thread_id), langgraph_thread_id=thread_id
    ):
        projected = saver.get_tuple(checkpoint_config)
    assert projected is not None
    restored_messages = projected.checkpoint["channel_values"]["messages"]
    assert len(restored_messages) == 2
    for message in restored_messages:
        metadata = message.response_metadata["message_metadata"]
        assert metadata["turn_id"] == USER_TURN_ID
        assert "job_id" not in metadata

    # 纯 prefix replay 只复用 catalog attribution，不读取当前 execution admission。
    owner = _owner(SESSION_ID, thread_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
        saver.put(
            checkpoint_config,
            _checkpoint(
                "checkpoint-user-prefix-replay",
                [user_message, assistant_message],
            ),
            {"source": "unit", "step": "prefix-replay"},
            {"messages": "prefix-replay"},
        )
    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT message_id, execution_id FROM messages ORDER BY message_sequence",
    ) == [
        ("accepted-user-root", USER_EXECUTION_ID),
        ("accepted-final", USER_EXECUTION_ID),
    ]


def test_internal_notice_and_tool_outputs_remain_turnless_and_do_not_reopen_final_turn(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    storage, saver, thread_id = _make_saver(tmp_path, session_bundle_factory)
    user_admission, user_message = _user_admission(saver, thread_id)
    final_message = AIMessage(content="completed answer", id="completed-final")
    user_config = _put_with_admission(
        saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        admission=user_admission,
        config=build_checkpoint_config(thread_id),
        checkpoint_id="checkpoint-completed-turn",
        messages=[user_message, final_message],
    )
    saver.finalize_turn(
        session_id=SESSION_ID,
        thread_id=thread_id,
        turn_id=USER_TURN_ID,
        final_message_id="completed-final",
    )

    internal_execution_id = "execution-internal-attribution"
    internal_admission = saver.admit_internal_execution(
        SESSION_ID,
        thread_id=thread_id,
        execution_id=internal_execution_id,
        job_id="job-internal-attribution",
        admission_idempotency_key="admission-internal-attribution",
        payload_hash=sha256_jcs({"notice": "child completed"}),
    )
    prompt = internal_message_factory.build(
        kind="generated_session_result",
        control="汇报已完成的内部子任务。",
        sections=(
            PromptSection("control_context", {"generated_session_id": "child-session"}),
            PromptSection("generated_session_result", "子任务结果正文。"),
        ),
        display_content="内部子任务已完成。",
    )
    internal_messages = [
        HumanMessage(
            content=prompt.content,
            id="internal-result-notice",
            response_metadata=prompt.metadata,
        ),
        AIMessage(
            content="",
            id="internal-tool-call",
            tool_calls=[
                {"id": "call-internal-attribution", "name": "read_file", "args": {}}
            ],
        ),
        ToolMessage(
            content="tool result",
            id="internal-tool-result",
            tool_call_id="call-internal-attribution",
            name="read_file",
        ),
        AIMessage(content="internal execution complete", id="internal-assistant-output"),
    ]
    _put_with_admission(
        saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        admission=internal_admission,
        config=user_config,
        checkpoint_id="checkpoint-internal-execution",
        messages=[user_message, final_message, *internal_messages],
    )

    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT message_id, turn_id, execution_id FROM messages "
        "WHERE execution_id = ? ORDER BY message_sequence",
        (internal_execution_id,),
    ) == [
        ("internal-result-notice", None, internal_execution_id),
        ("internal-tool-call", None, internal_execution_id),
        ("internal-tool-result", None, internal_execution_id),
        ("internal-assistant-output", None, internal_execution_id),
    ]
    internal_items = _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT semantic_kind, turn_id, turn_scope, execution_id FROM item_catalog "
        "WHERE execution_id = ? ORDER BY item_sequence",
        (internal_execution_id,),
    )
    assert internal_items
    assert all(row[1] is None for row in internal_items)
    assert all(
        row[2] is None
        for row in internal_items
        if row[0] in {"assistant_output", "tool_call", "tool_result"}
    )
    assert any(row[0] == "runtime_notice" for row in internal_items)
    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT final_message_id FROM turns WHERE turn_id = ?",
        (USER_TURN_ID,),
    ) == [("completed-final",)]


def test_new_checkpoint_message_requires_admission_and_rejects_cross_thread_owner(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    storage, saver, thread_id = _make_saver(
        tmp_path,
        session_bundle_factory,
        session_ids=(SESSION_ID, OTHER_SESSION_ID),
    )
    owner = _owner(SESSION_ID, thread_id)
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        pytest.raises(RuntimeError, match="缺少显式 execution admission"),
    ):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "checkpoint-missing-admission",
                [AIMessage(content="must be admitted", id="missing-admission")],
            ),
            {"source": "unit", "step": "missing-admission"},
            {"messages": "missing-admission"},
        )

    other_thread_id = get_session_path_resolver(
        saver._storage.sessions_dir
    ).main_thread_id(OTHER_SESSION_ID)
    other_owner = _owner(OTHER_SESSION_ID, other_thread_id)
    with (
        session_thread_owner_scope(
            other_owner,
            langgraph_thread_id=other_thread_id,
        ),
        pytest.raises(RuntimeError, match="SessionThread owner.*冲突"),
    ):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "checkpoint-cross-thread-owner",
                [AIMessage(content="wrong owner", id="cross-thread-owner")],
            ),
            {"source": "unit", "step": "cross-thread-owner"},
            {"messages": "cross-thread-owner"},
        )

    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT message_id FROM messages",
    ) == []
    jsonl = storage.jsonl_path(SESSION_ID, thread_id=thread_id)
    assert jsonl.read_bytes() == b""


def test_checkpoint_append_rolls_back_jsonl_and_attribution_on_projection_failure(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, saver, thread_id = _make_saver(tmp_path, session_bundle_factory)
    admission = saver.admit_internal_execution(
        SESSION_ID,
        thread_id=thread_id,
        execution_id="execution-rollback-attribution",
        job_id="job-rollback-attribution",
        admission_idempotency_key="admission-rollback-attribution",
        payload_hash=sha256_jcs({"rollback": "projection"}),
    )
    jsonl = storage.jsonl_path(SESSION_ID, thread_id=thread_id)
    original_jsonl = jsonl.read_bytes()

    def fail_projection(connection, sequence, value, timestamp) -> None:
        raise RuntimeError("message projection failure")

    monkeypatch.setattr(storage, "_insert_message_projection", fail_projection)
    owner = _owner(SESSION_ID, thread_id)
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
        pytest.raises(RuntimeError, match="message projection failure"),
    ):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "checkpoint-rollback-attribution",
                [AIMessage(content="transactional output", id="rollback-output")],
            ),
            {"source": "unit", "step": "rollback"},
            {"messages": "rollback"},
        )

    assert jsonl.read_bytes() == original_jsonl
    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT message_id FROM messages",
    ) == []
    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT item_id, execution_id FROM item_catalog",
    ) == []
