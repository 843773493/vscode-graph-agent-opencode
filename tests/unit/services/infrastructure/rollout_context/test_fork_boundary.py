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
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
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

SOURCE_SESSION_ID = "ses_019c3635a6c278efb831fe265bdcd656"
TARGET_SESSION_ID = "ses_019c40c2b8e9796588b3e3c86dee2e44"


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
    thread = saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)
    if step == 1:
        saver.accept_turn(
            SOURCE_SESSION_ID, thread_id=thread, accepted_ingress_id="user-1",
            acceptance_idempotency_key="boundary-accept", job_id="boundary-job",
            admission_idempotency_key="boundary-admission", payload="读取配置",
            turn_id="boundary-turn", root_item_id="item-user-1",
        )
    admission = saver.execution_admission(
        SOURCE_SESSION_ID, thread_id=thread,
        execution_id=saver.execution_for_turn(
            SOURCE_SESSION_ID, thread_id=thread, turn_id="boundary-turn",
        ),
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(SOURCE_SESSION_ID, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
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
        build_checkpoint_config(saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)),
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
        build_checkpoint_config(saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)),
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
    storage = RolloutContextStore(
        sessions_dir,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )
    return RolloutCheckpointSaver(sessions_dir, storage=storage)


async def _target_checkpoint(saver: RolloutCheckpointSaver):
    thread = saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    with session_thread_owner_scope(
        MutationIntentOwner(TARGET_SESSION_ID, thread), langgraph_thread_id=thread,
    ):
        return await saver.aget_tuple(build_checkpoint_config(thread))


async def test_context_fork_conflict_blocks_target_materialization(
    fork_saver: RolloutCheckpointSaver,
) -> None:
    _seed_pending_tool_call(fork_saver)
    source_manifest_before = fork_saver._storage.storage.initialize(SOURCE_SESSION_ID)
    assert await _target_checkpoint(fork_saver) is None

    with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
        await fork_saver.forks.afork(
            source_session_id=SOURCE_SESSION_ID,
            target_session_id=TARGET_SESSION_ID,
            mode="context_fork",
            checkpoint_id="checkpoint-2",
        )
    assert exc_info.value.code == CONFLICT_CODE

    # target staging/checkpoint 零副作用；source view 状态零变化。
    assert await _target_checkpoint(fork_saver) is None
    assert fork_saver._storage.storage.initialize(SOURCE_SESSION_ID) == source_manifest_before


async def test_history_prefix_fork_conflict_blocks_target_materialization(
    fork_saver: RolloutCheckpointSaver,
) -> None:
    _seed_pending_tool_call(fork_saver)

    with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
        await fork_saver.forks.afork(
            source_session_id=SOURCE_SESSION_ID,
            target_session_id=TARGET_SESSION_ID,
            mode="history_prefix_fork",
            checkpoint_id="checkpoint-2",
        )
    assert exc_info.value.code == CONFLICT_CODE
    assert await _target_checkpoint(fork_saver) is None


async def test_preflight_fork_conflict_rejects_before_session_creation(
    fork_saver: RolloutCheckpointSaver,
) -> None:
    _seed_pending_tool_call(fork_saver)
    source_manifest_before = fork_saver._storage.storage.initialize(SOURCE_SESSION_ID)

    with pytest.raises(ToolProtocolBoundaryConflict) as exc_info:
        await fork_saver.forks.source.preflight_fork(
            source_session_id=SOURCE_SESSION_ID,
            mode="context_fork",
            checkpoint_id="checkpoint-2",
        )
    assert exc_info.value.code == CONFLICT_CODE
    assert fork_saver._storage.storage.initialize(SOURCE_SESSION_ID) == source_manifest_before


async def test_context_fork_closed_boundary_succeeds(
    fork_saver: RolloutCheckpointSaver,
) -> None:
    _seed_closed_tool_group(fork_saver)
    result = await fork_saver.forks.afork(
        source_session_id=SOURCE_SESSION_ID,
        target_session_id=TARGET_SESSION_ID,
        mode="context_fork",
        checkpoint_id="checkpoint-2",
    )
    assert result.source_checkpoint_id == "checkpoint-2"

    target_tuple = await _target_checkpoint(fork_saver)
    assert target_tuple is not None
    target_messages = target_tuple.checkpoint["channel_values"]["messages"]
    source_message_ids = ("user-1", "assistant-call-1", "tool-result-1")
    source_thread = fork_saver._storage.storage.main_thread_id(SOURCE_SESSION_ID)
    target_thread = fork_saver._storage.storage.main_thread_id(TARGET_SESSION_ID)
    source_items = {
        item.projection_identity.projection_message_id: item
        for item in fork_saver._storage.storage.read_items(SOURCE_SESSION_ID, source_thread)
        if item.projection_identity is not None
        and item.projection_identity.projection_message_id in source_message_ids
    }
    target_items = {
        item.projection_identity.projection_message_id: item
        for item in fork_saver._storage.storage.read_items(TARGET_SESSION_ID, target_thread)
        if item.projection_identity is not None
        and item.projection_identity.projection_message_id is not None
    }
    assert set(source_items) == set(source_message_ids)
    assert len(target_messages) == len(source_message_ids) == len(target_items)
    assert len({message.id for message in target_messages}) == 3
    assert not set(target_items) & set(source_message_ids)
    for message, source_message_id in zip(target_messages, source_message_ids, strict=True):
        assert message.id.startswith(f"fork-message:{TARGET_SESSION_ID}:")
        item = target_items[message.id]
        origins = [
            relation for relation in item.provenance_relations
            if relation.relation == "derived_from"
            and relation.target_kind == "canonical_item"
        ]
        assert len(origins) == 1
        assert origins[0].target_id == source_items[source_message_id].item_id
        assert origins[0].target_session_id == SOURCE_SESSION_ID
        assert origins[0].target_thread_id == source_thread
    user, call, tool_result = target_messages
    assert isinstance(user, HumanMessage) and user.content == "读取配置"
    assert isinstance(call, AIMessage) and call.content == ""
    assert len(call.tool_calls) == 1
    assert call.tool_calls[0]["name"] == "read_file"
    assert call.tool_calls[0]["args"] == {"path": "config.json"}
    assert call.tool_calls[0]["id"] != "call-1"
    assert isinstance(tool_result, ToolMessage)
    assert tool_result.content == '{"ok":true}' and tool_result.name == "read_file"
    assert tool_result.tool_call_id == call.tool_calls[0]["id"]
