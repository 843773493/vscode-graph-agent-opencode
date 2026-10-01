"""C7-B3-B focused：fork 边界的 tool protocol closure preflight。

context_fork / history_prefix_fork 在 target staging 物化前经唯一
validate_tool_protocol_closure 验证 source view 前缀；冲突时零 target
副作用、source 零变化，闭合成功保持既有 fork 行为。
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

SOURCE_SESSION_ID = "ses_700882fcaad94e7caa0555eca9273d35"
TARGET_SESSION_ID = "ses_0b3f5c2d9a1e4f6087aa2b7c3d5e6f80"


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


def _seed_pending_tool_call(saver: RolloutCheckpointSaver) -> None:
    """checkpoint-1 只有 user；checkpoint-2 追加未收到 result 的 tool call。"""
    user = HumanMessage(content="读取配置", id="user-1")
    first_config = _put_checkpoint(
        saver,
        build_checkpoint_config(SOURCE_SESSION_ID),
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


def _seed_closed_tool_group(saver: RolloutCheckpointSaver) -> None:
    """checkpoint-2 追加完整 call 与 terminal result。"""
    user = HumanMessage(content="读取配置", id="user-1")
    first_config = _put_checkpoint(
        saver,
        build_checkpoint_config(SOURCE_SESSION_ID),
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


@pytest.fixture
def fork_saver(
    tmp_path: Path,
    session_bundle_factory,
) -> RolloutCheckpointSaver:
    sessions_dir = tmp_path / "sessions"
    for session_id in (SOURCE_SESSION_ID, TARGET_SESSION_ID):
        session_bundle_factory(sessions_dir, session_id)
    storage = RolloutStorage(
        sessions_dir,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )
    return RolloutCheckpointSaver(sessions_dir, storage=storage)


async def test_context_fork_conflict_blocks_target_materialization(
    fork_saver: RolloutCheckpointSaver,
) -> None:
    _seed_pending_tool_call(fork_saver)
    source_manifest_before = fork_saver._storage.initialize(SOURCE_SESSION_ID)
    target_config = build_checkpoint_config(TARGET_SESSION_ID)
    assert await fork_saver.aget_tuple(target_config) is None

    with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
        await fork_saver.afork(
            source_session_id=SOURCE_SESSION_ID,
            target_session_id=TARGET_SESSION_ID,
            mode="context_fork",
            checkpoint_id="checkpoint-2",
        )
    assert exc_info.value.code == CONFLICT_CODE

    # target staging/checkpoint 零副作用；source view 状态零变化。
    assert await fork_saver.aget_tuple(target_config) is None
    assert fork_saver._storage.initialize(SOURCE_SESSION_ID) == source_manifest_before


async def test_history_prefix_fork_conflict_blocks_target_materialization(
    fork_saver: RolloutCheckpointSaver,
) -> None:
    _seed_pending_tool_call(fork_saver)
    target_config = build_checkpoint_config(TARGET_SESSION_ID)

    with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
        await fork_saver.afork(
            source_session_id=SOURCE_SESSION_ID,
            target_session_id=TARGET_SESSION_ID,
            mode="history_prefix_fork",
            checkpoint_id="checkpoint-2",
        )
    assert exc_info.value.code == CONFLICT_CODE
    assert await fork_saver.aget_tuple(target_config) is None


async def test_preflight_fork_conflict_rejects_before_session_creation(
    fork_saver: RolloutCheckpointSaver,
) -> None:
    _seed_pending_tool_call(fork_saver)
    source_manifest_before = fork_saver._storage.initialize(SOURCE_SESSION_ID)

    with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
        await fork_saver.preflight_fork(
            source_session_id=SOURCE_SESSION_ID,
            mode="context_fork",
            checkpoint_id="checkpoint-2",
        )
    assert exc_info.value.code == CONFLICT_CODE
    assert fork_saver._storage.initialize(SOURCE_SESSION_ID) == source_manifest_before


async def test_context_fork_closed_boundary_succeeds(
    fork_saver: RolloutCheckpointSaver,
) -> None:
    _seed_closed_tool_group(fork_saver)
    result = await fork_saver.afork(
        source_session_id=SOURCE_SESSION_ID,
        target_session_id=TARGET_SESSION_ID,
        mode="context_fork",
        checkpoint_id="checkpoint-2",
    )
    assert result.source_checkpoint_id == "checkpoint-2"

    target_tuple = await fork_saver.aget_tuple(build_checkpoint_config(TARGET_SESSION_ID))
    assert target_tuple is not None
    target_messages = target_tuple.checkpoint["channel_values"]["messages"]
    assert [message.id for message in target_messages] == [
        "user-1",
        "assistant-call-1",
        "tool-result-1",
    ]
