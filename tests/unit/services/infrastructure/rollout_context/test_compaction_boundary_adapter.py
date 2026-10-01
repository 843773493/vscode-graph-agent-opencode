"""compaction 边界 typed adapter 的合同测试。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.services.infrastructure.rollout_context.checkpoint.compaction_boundary_adapter import (
    CompactionPreflightPort,
    validate_compaction_prefix_cutoffs,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.checkpoint.tool_protocol_boundary import (
    CONFLICT_CODE,
    ToolProtocolBoundaryConflict,
)
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage

SESSION_ID = "ses_700882fcaad94e7caa0555eca9273d35"


def _connection_with_pairing() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE tool_calls ("
        "assistant_message_sequence INTEGER, result_message_sequence INTEGER)"
    )
    # call 在 sequence 2，result 在 sequence 4：切点 2/3 会拆散配对。
    connection.execute("INSERT INTO tool_calls VALUES (2, 4)")
    return connection


def test_conflict_raises_with_code_and_zero_side_effect() -> None:
    connection = _connection_with_pairing()
    before = connection.execute(
        "SELECT assistant_message_sequence, result_message_sequence FROM tool_calls"
    ).fetchall()
    with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
        validate_compaction_prefix_cutoffs(connection, [1, 2, 3, 4], [3])
    assert exc_info.value.code == CONFLICT_CODE
    after = connection.execute(
        "SELECT assistant_message_sequence, result_message_sequence FROM tool_calls"
    ).fetchall()
    assert after == before


def test_closed_boundaries_pass() -> None:
    connection = _connection_with_pairing()
    validate_compaction_prefix_cutoffs(connection, [1, 2, 3, 4], [1, 4])


def _checkpoint(checkpoint_id: str, messages: list[object]) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {
        channel: str(index + 1)
        for index, channel in enumerate(checkpoint["channel_values"])
    }
    checkpoint["updated_channels"] = list(checkpoint["channel_values"])
    return checkpoint


def _put_checkpoint(
    saver: RolloutCheckpointSaver,
    config: object,
    checkpoint_id: str,
    messages: list[object],
    step: int,
) -> object:
    return saver.put(
        config,
        _checkpoint(checkpoint_id, messages),
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
        build_checkpoint_config(SESSION_ID),
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
    storage = RolloutStorage(
        sessions_dir,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )
    return RolloutCheckpointSaver(sessions_dir, storage=storage)


def test_safe_cutoffs_exclude_pair_splitting_prefixes(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    saver = _make_saver(tmp_path, session_bundle_factory)
    user, call, result = _seed_closed_tool_group(saver)
    state = [user, call, result]
    # 切点 2 会把 durable call 与 terminal result 拆到边界两侧。
    assert saver.safe_compaction_prefix_cutoffs(
        SESSION_ID,
        checkpoint_ns="",
        state_messages=state,
        cutoff_indexes=[0, 1, 2, 3],
    ) == frozenset({0, 1, 3})


def test_safe_cutoffs_reject_out_of_range_candidate(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    saver = _make_saver(tmp_path, session_bundle_factory)
    user, call, result = _seed_closed_tool_group(saver)
    with pytest.raises(ValueError, match="切点超出 state 消息范围"):
        saver.safe_compaction_prefix_cutoffs(
            SESSION_ID,
            checkpoint_ns="",
            state_messages=[user, call, result],
            cutoff_indexes=[4],
        )


def test_safe_cutoffs_fail_closed_on_inflight_open_group(
    tmp_path: Path,
    session_bundle_factory,
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
        SESSION_ID,
        checkpoint_ns="",
        state_messages=state,
        cutoff_indexes=[3, 4, 5],
    ) == frozenset({3, 5})


def test_safe_cutoffs_require_active_checkpoint(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    saver = _make_saver(tmp_path, session_bundle_factory)
    with pytest.raises(RuntimeError, match="缺少 durable owner"):
        saver.safe_compaction_prefix_cutoffs(
            SESSION_ID,
            checkpoint_ns="",
            state_messages=[HumanMessage(content="hello", id="user-1")],
            cutoff_indexes=[0],
        )


def test_saver_satisfies_typed_preflight_port(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    saver = _make_saver(tmp_path, session_bundle_factory)
    port: CompactionPreflightPort = saver
    user, call, result = _seed_closed_tool_group(saver)
    assert port.safe_compaction_prefix_cutoffs(
        SESSION_ID,
        checkpoint_ns="",
        state_messages=[user, call, result],
        cutoff_indexes=[0, 3],
    ) == frozenset({0, 3})
