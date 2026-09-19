"""C7-B2 focused：tool protocol boundary preflight。

唯一公共 validator（tool_protocol_boundary）接入 create_context_boundary
chokepoint；rewind（含 replay 使用的 arewind/arewind_to_turn）经由同一
入口验证冲突零副作用与闭合边界成功。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
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


def _checkpoint(
    checkpoint_id: str,
    messages: list[object],
) -> dict[str, object]:
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


def _seed_closed_tool_group(saver: RolloutCheckpointSaver) -> None:
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


def test_rewind_conflict_keeps_old_view_zero_side_effects(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = RolloutStorage(
        sessions_dir, serde=JsonPlusSerializer(), message_codec=LangChainMessageCodec()
    )
    saver = RolloutCheckpointSaver(sessions_dir, storage=storage)
    _seed_closed_tool_group(saver)
    manifest_before = storage.initialize(SESSION_ID)

    with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
        storage.rewind_to_checkpoint(
            thread_id=SESSION_ID,
            checkpoint_id="checkpoint-2",
            source_anchor="assistant-call-1",
            anchor_mode="inclusive",
        )
    error = exc_info.value
    assert error.code == CONFLICT_CODE
    # 安全 anchor 候选：call 之前的完整前缀，以及包含全部 call+result 的 view
    cutoff_indices = [anchor.cutoff_index for anchor in error.safe_anchors]
    assert 1 in cutoff_indices
    assert 3 in cutoff_indices
    for anchor in error.safe_anchors:
        assert (
            anchor.cutoff_message_sequence is None
            or type(anchor.cutoff_message_sequence) is int
        )

    manifest_after = storage.initialize(SESSION_ID)
    assert manifest_after == manifest_before


def test_rewind_conflict_for_pending_call_has_no_safe_anchor_above(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = RolloutStorage(
        sessions_dir, serde=JsonPlusSerializer(), message_codec=LangChainMessageCodec()
    )
    saver = RolloutCheckpointSaver(sessions_dir, storage=storage)
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
    _put_checkpoint(
        saver,
        first_config,
        "checkpoint-2",
        [user, call],
        2,
    )
    manifest_before = storage.initialize(SESSION_ID)

    with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
        storage.rewind_to_checkpoint(
            thread_id=SESSION_ID,
            checkpoint_id="checkpoint-2",
            source_anchor="assistant-call-1",
            anchor_mode="inclusive",
        )
    # pending call 没有任何 terminal result：包含 call 的前缀全部未闭合，
    # 因此只有 call 之前的候选，没有更大的安全切点。
    assert [anchor.cutoff_index for anchor in exc_info.value.safe_anchors] == [1]
    assert storage.initialize(SESSION_ID) == manifest_before


def test_rewind_at_closed_boundaries_succeeds(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = RolloutStorage(
        sessions_dir, serde=JsonPlusSerializer(), message_codec=LangChainMessageCodec()
    )
    saver = RolloutCheckpointSaver(sessions_dir, storage=storage)
    _seed_closed_tool_group(saver)
    manifest_before = storage.initialize(SESSION_ID)

    # 包含完整 call+result 的闭合边界：rewind 成功并推进 view revision。
    manifest = storage.rewind_to_checkpoint(
        thread_id=SESSION_ID,
        checkpoint_id="checkpoint-2",
        source_anchor="tool-result-1",
        anchor_mode="inclusive",
    )
    assert manifest.history_view_revision == manifest_before.history_view_revision + 1

    # 位于 tool-call group 之前的 user 锚点同样是闭合边界。
    manifest_user = storage.rewind_to_checkpoint(
        thread_id=SESSION_ID,
        checkpoint_id="checkpoint-1",
        source_anchor="user-1",
        anchor_mode="inclusive",
    )
    assert (
        manifest_user.history_view_revision == manifest.history_view_revision + 1
    )
