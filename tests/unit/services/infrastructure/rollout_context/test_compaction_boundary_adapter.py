"""compaction 边界 typed adapter 的合同测试。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.boundary.compaction_boundary_adapter import (
    CompactionPreflightPort,
    validate_compaction_prefix_cutoffs,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    CONFLICT_CODE,
    ToolProtocolBoundaryConflict,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore

SESSION_ID = "ses_019c3635a6c278efb831fe265bdcd656"


def test_conflict_raises_with_code_and_zero_side_effect(
    paired_saver: RolloutCheckpointSaver,
) -> None:
    storage = paired_saver._storage.storage
    thread = storage.main_thread_id(SESSION_ID)
    jsonl = storage.jsonl_path(SESSION_ID, thread_id=thread)
    before_jsonl = jsonl.read_bytes()
    with storage.open_read_snapshot(SESSION_ID, thread) as snapshot:
        items = paired_saver._storage.checkpoints.committed_context_items(
            snapshot, predecessor=paired_saver._storage.assemblies.latest_context_assembly(snapshot),
        )
        sequences = paired_saver._storage.checkpoints.selected_context_message_sequences(snapshot, items=items)
        before = tuple(snapshot.connection.iterdump())
        # call 位于 2，result 位于 4；压缩切点 3 必须拒绝真实 typed pair。
        with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
            validate_compaction_prefix_cutoffs(snapshot.connection, sequences, [3])
        assert exc_info.value.code == CONFLICT_CODE
        assert tuple(snapshot.connection.iterdump()) == before
    assert jsonl.read_bytes() == before_jsonl


def test_closed_boundaries_pass(paired_saver: RolloutCheckpointSaver) -> None:
    storage = paired_saver._storage.storage
    thread = storage.main_thread_id(SESSION_ID)
    with storage.open_read_snapshot(SESSION_ID, thread) as snapshot:
        items = paired_saver._storage.checkpoints.committed_context_items(
            snapshot, predecessor=paired_saver._storage.assemblies.latest_context_assembly(snapshot),
        )
        sequences = paired_saver._storage.checkpoints.selected_context_message_sequences(snapshot, items=items)
        validate_compaction_prefix_cutoffs(snapshot.connection, sequences, [1, 4])


def _checkpoint(checkpoint_id: str, messages: list[object], version: str) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": version}
    checkpoint["updated_channels"] = list(checkpoint["channel_values"])
    return checkpoint


def _put_checkpoint(
    saver: RolloutCheckpointSaver,
    config: object,
    checkpoint_id: str,
    messages: list[object],
    step: int,
) -> object:
    thread = saver._storage.storage.main_thread_id(SESSION_ID)
    if step == 1:
        saver.accept_turn(
            SESSION_ID, thread_id=thread, accepted_ingress_id="user-1",
            acceptance_idempotency_key="boundary-accept", job_id="boundary-job",
            admission_idempotency_key="boundary-admission", payload="读取配置",
            turn_id="boundary-turn", root_item_id="item-user-1",
        )
    admission = saver.execution_admission(
        SESSION_ID, thread_id=thread,
        execution_id=saver.execution_for_turn(
            SESSION_ID, thread_id=thread, turn_id="boundary-turn",
        ),
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(SESSION_ID, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        return saver.put(
            config,
            _checkpoint(checkpoint_id, messages, str(step)),
            {"source": "job", "step": step},
            {"messages": str(step)},
        )


def _seed_closed_tool_group(
    saver: RolloutCheckpointSaver,
) -> tuple[HumanMessage, AIMessage, ToolMessage]:
    """checkpoint-1 只有 user；checkpoint-2 追加 call 与 terminal result。"""
    user = HumanMessage(content="读取配置", id="user-1")
    first_config = _put_checkpoint(
        saver,
        build_checkpoint_config(saver._storage.storage.main_thread_id(SESSION_ID)),
        "checkpoint-1",
        [user],
        1,
    )
    call = AIMessage(
        content="",
        id="assistant-call-1",
        tool_calls=[
            {"name": "read_file", "args": {"path": "config.json"}, "id": "call-1"}
        ],
    )
    result = ToolMessage(
        content='{"ok":true}',
        id="tool-result-1",
        tool_call_id="call-1",
        name="read_file",
    )
    _put_checkpoint(
        saver,
        first_config,
        "checkpoint-2",
        [user, call, result],
        2,
    )
    return user, call, result


def _make_saver(
    tmp_path: Path,
    session_bundle_factory: Callable[[Path, str], Path],
) -> RolloutCheckpointSaver:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = RolloutContextStore(
        sessions_dir,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )
    return RolloutCheckpointSaver(sessions_dir, storage=storage)


@pytest.fixture
def paired_saver(tmp_path: Path, session_bundle_factory) -> RolloutCheckpointSaver:
    saver = _make_saver(tmp_path, session_bundle_factory)
    user = HumanMessage(content="读取配置", id="user-1")
    call = AIMessage(content="", id="assistant-call-1", tool_calls=[
        {"name": "read_file", "args": {"path": "config.json"}, "id": "call-1"},
    ])
    result = ToolMessage(content='{"ok":true}', id="tool-result-1", tool_call_id="call-1", name="read_file")
    _put_checkpoint(
        saver, build_checkpoint_config(saver._storage.storage.main_thread_id(SESSION_ID)),
        "checkpoint-with-middle", [user, call, AIMessage(content="等待工具结果", id="middle-1"), result], 1,
    )
    return saver


@pytest.mark.parametrize("checkpoint_ns", ["", "model:provider-call"])
def test_safe_cutoffs_exclude_pair_splitting_prefixes(
    tmp_path: Path,
    session_bundle_factory,
    checkpoint_ns: str,
) -> None:
    saver = _make_saver(tmp_path, session_bundle_factory)
    user, call, result = _seed_closed_tool_group(saver)
    state = [user, call, result]
    storage = saver._storage.storage
    thread = storage.main_thread_id(SESSION_ID)
    with storage.read_connection(SESSION_ID, thread, "") as connection:
        before = tuple(connection.iterdump())
    jsonl = storage.jsonl_path(SESSION_ID, thread_id=thread)
    before_jsonl = jsonl.read_bytes()
    # 切点 2 会把 durable call 与 terminal result 拆到边界两侧。
    assert saver.safe_compaction_prefix_cutoffs(
        SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID),
        checkpoint_ns=checkpoint_ns,
        state_messages=state,
        cutoff_indexes=[0, 1, 2, 3],
    ) == frozenset({0, 1, 3})
    with storage.read_connection(SESSION_ID, thread, "") as connection:
        assert tuple(connection.iterdump()) == before
    assert jsonl.read_bytes() == before_jsonl


@pytest.mark.parametrize("checkpoint_ns", ["", "model:provider-call"])
def test_safe_cutoffs_reject_out_of_range_candidate(
    tmp_path: Path,
    session_bundle_factory,
    checkpoint_ns: str,
) -> None:
    saver = _make_saver(tmp_path, session_bundle_factory)
    user, call, result = _seed_closed_tool_group(saver)
    with pytest.raises(ValueError, match="切点超出 state 消息范围"):
        saver.safe_compaction_prefix_cutoffs(
            SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID),
            checkpoint_ns=checkpoint_ns,
            state_messages=[user, call, result],
            cutoff_indexes=[4],
        )


@pytest.mark.parametrize("checkpoint_ns", ["", "model:provider-call"])
def test_safe_cutoffs_fail_closed_on_inflight_open_group(
    tmp_path: Path,
    session_bundle_factory,
    checkpoint_ns: str,
) -> None:
    saver = _make_saver(tmp_path, session_bundle_factory)
    user, call, result = _seed_closed_tool_group(saver)
    inflight_call = AIMessage(
        content="",
        id="assistant-call-2",
        tool_calls=[
            {"name": "read_file", "args": {"path": "a.json"}, "id": "call-2"}
        ],
    )
    inflight_result = ToolMessage(
        content='{"ok":true}',
        id="tool-result-2",
        tool_call_id="call-2",
        name="read_file",
    )
    state = [user, call, result, inflight_call, inflight_result]
    # in-flight call 未闭合的切点 4 必须被排除；闭合切点 3/5 保留。
    assert saver.safe_compaction_prefix_cutoffs(
        SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID),
        checkpoint_ns=checkpoint_ns,
        state_messages=state,
        cutoff_indexes=[3, 4, 5],
    ) == frozenset({3, 5})


@pytest.mark.parametrize("checkpoint_ns", ["", "model:provider-call"])
def test_safe_cutoffs_require_active_checkpoint(
    tmp_path: Path,
    session_bundle_factory,
    checkpoint_ns: str,
) -> None:
    saver = _make_saver(tmp_path, session_bundle_factory)
    thread = saver._storage.storage.main_thread_id(SESSION_ID)
    # 先由正式 writer 物化空 rollout，才能测试“存在 owner、缺少 active checkpoint”。
    saver._storage.storage.initialize(SESSION_ID, thread_id=thread)
    with pytest.raises(RuntimeError, match="缺少 durable owner"):
        saver.safe_compaction_prefix_cutoffs(
            SESSION_ID, thread,
            checkpoint_ns=checkpoint_ns,
            state_messages=[HumanMessage(content="hello", id="user-1")],
            cutoff_indexes=[0],
        )


@pytest.mark.parametrize("checkpoint_ns", ["", "model:provider-call"])
def test_saver_satisfies_typed_preflight_port(
    tmp_path: Path,
    session_bundle_factory,
    checkpoint_ns: str,
) -> None:
    saver = _make_saver(tmp_path, session_bundle_factory)
    port: CompactionPreflightPort = saver
    user, call, result = _seed_closed_tool_group(saver)
    assert port.safe_compaction_prefix_cutoffs(
        SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID),
        checkpoint_ns=checkpoint_ns,
        state_messages=[user, call, result],
        cutoff_indexes=[0, 3],
    ) == frozenset({0, 3})


@pytest.mark.parametrize("checkpoint_ns", ["named-owner", None, 7])
def test_safe_cutoffs_reject_invalid_owner_without_writes(
    tmp_path: Path,
    session_bundle_factory,
    checkpoint_ns: object,
) -> None:
    saver = _make_saver(tmp_path, session_bundle_factory)
    user, call, result = _seed_closed_tool_group(saver)
    storage = saver._storage.storage
    thread = storage.main_thread_id(SESSION_ID)
    with storage.read_connection(SESSION_ID, thread, "") as connection:
        before = tuple(connection.iterdump())
    jsonl = storage.jsonl_path(SESSION_ID, thread_id=thread)
    before_jsonl = jsonl.read_bytes()
    error = RuntimeError if isinstance(checkpoint_ns, str) else TypeError
    message = (
        "rollout checkpoint namespace 状态缺失: 'named-owner'"
        if isinstance(checkpoint_ns, str)
        else "compaction preflight checkpoint_ns 必须是字符串"
    )
    with pytest.raises(error, match=message):
        saver.safe_compaction_prefix_cutoffs(
            SESSION_ID,
            thread,
            checkpoint_ns=checkpoint_ns,  # type: ignore[arg-type]
            state_messages=[user, call, result],
            cutoff_indexes=[0, 3],
        )
    with storage.read_connection(SESSION_ID, thread, "") as connection:
        assert tuple(connection.iterdump()) == before
    assert jsonl.read_bytes() == before_jsonl
