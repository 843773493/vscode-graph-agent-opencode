"""真实 checkpoint writer 的 Turn/execution attribution 合同。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
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
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.prompting.factory import PromptSection, internal_message_factory
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from tests.support.rollout_output import commit_fixture_output

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
) -> tuple[RolloutContextStore, RolloutCheckpointSaver, str]:
    sessions_dir = tmp_path / "sessions"
    for session_id in session_ids:
        session_bundle_factory(sessions_dir, session_id)
    storage = RolloutContextStore(
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
    storage: RolloutContextStore,
    session_id: str,
    thread_id: str,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> list[tuple[object, ...]]:
    with sqlite3.connect(
        storage.storage.index_path(session_id, thread_id=thread_id)
    ) as connection:
        return connection.execute(sql, parameters).fetchall()


@dataclass
class HeadSelectionCase:
    saver: RolloutCheckpointSaver
    thread_id: str
    fixed: RunnableConfig
    admissions: dict[str, ExecutionAdmission]


@pytest.fixture
def head_selection_case(tmp_path: Path, session_bundle_factory) -> Iterator[HeadSelectionCase]:
    storage, saver, thread_id = _make_saver(
        tmp_path, session_bundle_factory, session_ids=(SESSION_ID, OTHER_SESSION_ID),
    )
    with saver:
        notice = internal_message_factory.build(
            kind="generated_session_result", control="汇报内部任务。",
            sections=(
                PromptSection("control_context", {}),
                PromptSection("generated_session_result", "内部任务结果"),
            ),
            display_content="内部任务已完成。",
        )

        def admit_internal(scope: str) -> ExecutionAdmission:
            return saver.admit_internal_execution(
                SESSION_ID, thread_id=thread_id, job_id=f"{scope}-job",
                intent=InternalExecutionIntent(
                    message_id=f"{scope}-notice", execution_id=f"{scope}-execution",
                    admission_idempotency_key=f"{scope}-admission",
                ),
                message=HumanMessage(
                    content=notice.content, id=f"{scope}-notice",
                    response_metadata={
                        "message_id": f"{scope}-notice",
                        "created_at": "2026-10-04T12:00:00+00:00",
                        "message_metadata": notice.metadata,
                    },
                ),
            )

        # 内部执行也按持久准入游标隔离未来输入；turn_id=None 不代表空历史。
        internal = admit_internal("internal-scope")
        current, _message = _user_admission(saver, thread_id)
        fixed = _put_with_admission(
            saver, session_id=SESSION_ID, thread_id=thread_id, admission=current,
            config=build_checkpoint_config(thread_id), checkpoint_id="checkpoint-before-accepted-input", messages=[],
        )
        future = saver.accept_turn(
            SESSION_ID, thread_id=thread_id, accepted_ingress_id="future-ingress",
            acceptance_idempotency_key="future-acceptance", job_id="future-job",
            admission_idempotency_key="future-admission", payload="future question",
            turn_id="future-turn", initial_execution_id="future-execution",
        )
        future_admission = saver.execution_admission(
            SESSION_ID, thread_id=thread_id, execution_id=future["initial_execution_id"],
        )
        internal_late = admit_internal("internal-late-scope")
        saver.accept_turn(
            OTHER_SESSION_ID, thread_id=storage.storage.main_thread_id(OTHER_SESSION_ID),
            accepted_ingress_id="other-scope-ingress", acceptance_idempotency_key="other-scope-acceptance",
            job_id="other-scope-job", admission_idempotency_key="other-scope-admission",
            payload="other scope question",
        )
        yield HeadSelectionCase(saver, thread_id, fixed, {
            "current": current, "future": future_admission, "internal": internal,
            "internal_late": internal_late,
        })


@pytest.mark.parametrize("scope, expected", [
    ("cold", ["accepted question", "future question"]),
    ("current", ["accepted question"]),
    ("future", ["accepted question", "future question"]),
    ("internal", []),
    ("internal_late", ["accepted question", "future question"]),
])
def test_latest_messages_keep_accepted_input_with_execution_cutoff(
    head_selection_case: HeadSelectionCase, scope: str, expected: list[str],
) -> None:
    case = head_selection_case
    with ExitStack() as stack:
        stack.enter_context(session_thread_owner_scope(_owner(SESSION_ID, case.thread_id), langgraph_thread_id=case.thread_id))
        if scope != "cold":
            stack.enter_context(execution_admission_scope(case.admissions[scope], langgraph_thread_id=case.thread_id))
        latest = case.saver.get_tuple(build_checkpoint_config(case.thread_id))
        assert latest is not None
        assert [message.content for message in latest.checkpoint["channel_values"]["messages"]] == expected
        fixed = case.saver.get_tuple(case.fixed)
        assert fixed is not None and fixed.checkpoint["channel_values"]["messages"] == []


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
            {"messages": "checkpoint-user-prefix-replay"},
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
    frozen_provider_prepare,
) -> None:
    storage, saver, thread_id = _make_saver(tmp_path, session_bundle_factory)
    user_admission, user_message = _user_admission(saver, thread_id)
    with (
        session_thread_owner_scope(_owner(SESSION_ID, thread_id), langgraph_thread_id=thread_id),
        execution_admission_scope(user_admission, langgraph_thread_id=thread_id),
    ):
        frozen_provider_prepare(saver, key="attribution-user-create")
    final_message = AIMessage(content="completed answer", id="completed-final")
    with (
        session_thread_owner_scope(_owner(SESSION_ID, thread_id), langgraph_thread_id=thread_id),
        execution_admission_scope(user_admission, langgraph_thread_id=thread_id),
    ):
        commit_fixture_output(saver, messages=[user_message, final_message])
    user_config = _put_with_admission(
        saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        admission=user_admission,
        config=build_checkpoint_config(thread_id),
        checkpoint_id="checkpoint-completed-turn",
        messages=[user_message, final_message],
    )

    internal_execution_id = "execution-internal-attribution"
    prompt = internal_message_factory.build(
        kind="generated_session_result",
        control="汇报已完成的内部子任务。",
        sections=(
            PromptSection("control_context", {"generated_session_id": "child-session"}),
            PromptSection("generated_session_result", "子任务结果正文。"),
        ),
        display_content="内部子任务已完成。",
    )
    internal_notice = HumanMessage(
        content=prompt.content,
        id="internal-result-notice",
        response_metadata={
            "message_id": "internal-result-notice",
            "created_at": "2026-10-04T12:00:00+00:00",
            "message_metadata": prompt.metadata,
        },
    )
    internal_admission = saver.admit_internal_execution(
        SESSION_ID,
        thread_id=thread_id,
        job_id="job-internal-attribution",
        intent=InternalExecutionIntent(
            message_id="internal-result-notice",
            execution_id=internal_execution_id,
            admission_idempotency_key="admission-internal-attribution",
        ),
        message=internal_notice,
    )
    internal_messages = [
        internal_notice,
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
            {"messages": "checkpoint-missing-admission"},
        )

    other_thread_id = get_session_path_resolver(
        saver._storage.storage.sessions_dir
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
            {"messages": "checkpoint-cross-thread-owner"},
        )

    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT message_id FROM messages",
    ) == []
    jsonl = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id)
    assert jsonl.read_bytes() == b""


def test_checkpoint_append_rolls_back_jsonl_and_attribution_on_projection_failure(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, saver, thread_id = _make_saver(tmp_path, session_bundle_factory)
    storage.storage.initialize(SESSION_ID, thread_id=thread_id)
    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT item_id, execution_id FROM item_catalog",
    ) == []
    internal_notice = HumanMessage(
        content="准备验证 checkpoint 消息投影失败的事务回滚。",
        id="rollback-admission-notice",
        response_metadata={
            "internal": True,
            "message_id": "rollback-admission-notice",
            "created_at": "2026-10-04T12:00:00+00:00",
        },
    )
    admission = saver.admit_internal_execution(
        SESSION_ID,
        thread_id=thread_id,
        job_id="job-rollback-attribution",
        intent=InternalExecutionIntent(
            message_id="rollback-admission-notice",
            execution_id="execution-rollback-attribution",
            admission_idempotency_key="admission-rollback-attribution",
        ),
        message=internal_notice,
    )
    jsonl = storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id)
    original_jsonl = jsonl.read_bytes()
    # admission notice 已提交；失败的 checkpoint 必须恢复这一真实前像。
    original_attribution = _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT item_id, execution_id FROM item_catalog",
    )
    assert original_attribution == [
        ("item-rollback-admission-notice", "execution-rollback-attribution"),
    ]
    assert _query(
        storage, SESSION_ID, thread_id, "SELECT message_id FROM messages"
    ) == [("rollback-admission-notice",)]
    original_messages = _query(
        storage, SESSION_ID, thread_id,
        "SELECT * FROM messages ORDER BY message_sequence",
    )
    original_projections = _query(
        storage, SESSION_ID, thread_id,
        "SELECT * FROM message_projections ORDER BY message_sequence",
    )

    def fail_projection(
        connection, sequence, value, timestamp, *, canonical_items: tuple[object, ...],
    ) -> None:
        raise RuntimeError("message projection failure")

    monkeypatch.setattr(storage.checkpoints, "_insert_message_projection", fail_projection)
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
            {"messages": "checkpoint-rollback-attribution"},
        )

    assert jsonl.read_bytes() == original_jsonl
    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT * FROM messages ORDER BY message_sequence",
    ) == original_messages
    assert _query(
        storage, SESSION_ID, thread_id,
        "SELECT * FROM message_projections ORDER BY message_sequence",
    ) == original_projections
    assert _query(
        storage,
        SESSION_ID,
        thread_id,
        "SELECT item_id, execution_id FROM item_catalog",
    ) == original_attribution
