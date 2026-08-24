from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.schemas.public_v2.session import SessionCreateRequest
from app.services.business.session_context_fork_service import (
    SessionContextForkService,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore


@dataclass(frozen=True, slots=True)
class ForkServices:
    session_service: SessionService
    fork_service: SessionContextForkService
    checkpointer: RolloutCheckpointSaver


@pytest.fixture
def fork_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ForkServices:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    checkpointer = RolloutCheckpointSaver(sessions_dir=sessions_dir)
    session_service = SessionService(
        config_service=ConfigService(workspace_root=tmp_path),
        trace_event_store=TraceEventStore(sessions_dir=sessions_dir),
        fork_relationship_checker=checkpointer,
    )
    return ForkServices(
        session_service=session_service,
        fork_service=SessionContextForkService(
            session_service=session_service,
            checkpointer=checkpointer,
        ),
        checkpointer=checkpointer,
    )


@pytest.mark.asyncio
async def test_fork_copies_agent_state_into_independent_child(
    fork_services: ForkServices,
) -> None:
    source = await fork_services.session_service.create(
        SessionCreateRequest(title="源会话")
    )
    checkpoint = {
        "v": 1,
        "id": "checkpoint-source",
        "ts": "2026-07-13T00:00:00+00:00",
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="记住代号 alpha",
                    id="source-user-1",
                    response_metadata={"turn_id": "turn-1"},
                ),
                AIMessage(content="已记住", id="source-final-1"),
            ],
            "scratchpad": {"current_task": "继续验证 alpha"},
        },
        "channel_versions": {"messages": 1, "scratchpad": 1},
        "versions_seen": {},
        "pending_sends": [],
        "updated_channels": ["messages", "scratchpad"],
    }
    await fork_services.checkpointer.aput(
        build_checkpoint_config(source.session_id),
        checkpoint,
        {"source": "loop", "step": 2, "parents": {}},
        {"messages": 1, "scratchpad": 1},
    )
    fork_services.checkpointer.finalize_turn(
        session_id=source.session_id,
        turn_id="turn-1",
        final_message_id="source-final-1",
    )

    child = await fork_services.fork_service.fork(source.session_id)

    assert child.parent_session_id is None
    assert child.context_source_session_id == source.session_id
    assert child.current_agent_id == source.current_agent_id
    assert child.title == "源会话（上下文副本）"
    assert child.title_source == "auto"
    source_path = fork_services.session_service.path_resolver.resolve_session_node(
        source.session_id
    )
    child_path = fork_services.session_service.path_resolver.resolve_session_node(
        child.session_id
    )
    assert child_path.parent == source_path.parent

    source_tuple = await fork_services.checkpointer.aget_tuple(
        build_checkpoint_config(source.session_id)
    )
    child_tuple = await fork_services.checkpointer.aget_tuple(
        build_checkpoint_config(child.session_id)
    )
    assert source_tuple is not None
    assert child_tuple is not None
    assert child_tuple.checkpoint["id"] != source_tuple.checkpoint["id"]
    source_values = source_tuple.checkpoint["channel_values"]
    child_values = child_tuple.checkpoint["channel_values"]
    assert child_values["scratchpad"] == source_values["scratchpad"]
    assert [message.content for message in child_values["messages"]] == [
        message.content for message in source_values["messages"]
    ]
    assert all(
        message.response_metadata["context_fork_source_session_id"] == source.session_id
        for message in child_values["messages"]
    )
    assert child_tuple.checkpoint.get("pending_sends") == []
    assert child_tuple.metadata["source"] == "fork"


@pytest.mark.asyncio
async def test_fork_empty_context_still_creates_bound_child(
    fork_services: ForkServices,
) -> None:
    source = await fork_services.session_service.create(
        SessionCreateRequest(title="空上下文")
    )

    child = await fork_services.fork_service.fork(source.session_id)

    assert child.parent_session_id is None
    assert (
        await fork_services.checkpointer.aget_tuple(
            build_checkpoint_config(child.session_id)
        )
        is None
    )


@pytest.mark.asyncio
async def test_detached_fork_survives_parent_deletion(
    fork_services: ForkServices,
) -> None:
    source = await fork_services.session_service.create(
        SessionCreateRequest(title="可删除源会话")
    )
    checkpoint = {
        "v": 1,
        "id": "checkpoint-source",
        "ts": "2026-07-13T00:00:00+00:00",
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="独立上下文",
                    id="u1",
                    response_metadata={"turn_id": "turn-1"},
                ),
                AIMessage(content="独立回答", id="a1"),
            ],
            "scratchpad": {"current_task": "继续"},
        },
        "channel_versions": {"messages": 1, "scratchpad": 1},
        "versions_seen": {},
        "pending_sends": [],
        "updated_channels": ["messages", "scratchpad"],
    }
    await fork_services.checkpointer.aput(
        build_checkpoint_config(source.session_id),
        checkpoint,
        {"source": "test", "step": 1, "parents": {}},
        {"messages": "1", "counter": "1"},
    )
    fork_services.checkpointer.finalize_turn(
        session_id=source.session_id,
        turn_id="turn-1",
        final_message_id="a1",
    )

    child = await fork_services.fork_service.fork(source.session_id)
    await fork_services.session_service.delete(source.session_id)

    restored = await fork_services.checkpointer.aget_tuple(
        build_checkpoint_config(child.session_id)
    )
    assert restored is not None
    assert restored.checkpoint["channel_values"]["messages"][0].content == "独立上下文"


@pytest.mark.asyncio
async def test_pinned_fork_blocks_parent_deletion(
    fork_services: ForkServices,
) -> None:
    source = await fork_services.session_service.create(
        SessionCreateRequest(title="固定源会话")
    )
    child = await fork_services.fork_service.fork(
        source.session_id,
        pinned=True,
        place_under_source=True,
    )

    with pytest.raises(RuntimeError, match=child.session_id):
        await fork_services.session_service.delete(source.session_id)


@pytest.mark.asyncio
async def test_history_prefix_fork_copies_checkpoints_without_parent_dependency(
    fork_services: ForkServices,
) -> None:
    source = await fork_services.session_service.create(
        SessionCreateRequest(title="历史前缀源会话")
    )
    first = {
        "v": 1,
        "id": "checkpoint-prefix-1",
        "ts": "2026-07-13T00:00:00+00:00",
        "channel_values": {
            "messages": [HumanMessage(content="第一轮", id="u1")],
            "scratchpad": {"step": 1},
        },
        "channel_versions": {"messages": "1", "scratchpad": "1"},
        "versions_seen": {},
        "pending_sends": [],
        "updated_channels": ["messages", "scratchpad"],
    }
    first_config = await fork_services.checkpointer.aput(
        build_checkpoint_config(source.session_id),
        first,
        {"source": "test", "step": 1, "parents": {}},
        {"messages": "1", "scratchpad": "1"},
    )
    second = {
        **first,
        "id": "checkpoint-prefix-2",
        "channel_values": {
            "messages": [
                HumanMessage(content="第一轮", id="u1"),
                AIMessage(content="第二轮输出", id="a2"),
            ],
            "scratchpad": {"step": 2},
        },
        "channel_versions": {"messages": "2", "scratchpad": "2"},
    }
    await fork_services.checkpointer.aput(
        first_config,
        second,
        {"source": "test", "step": 2, "parents": {}},
        {"messages": "2", "scratchpad": "2"},
    )

    child = await fork_services.fork_service.fork(
        source.session_id,
        mode="history_prefix_fork",
        checkpoint_id="checkpoint-prefix-2",
    )

    source_path = fork_services.session_service.path_resolver.resolve_session_node(
        source.session_id
    )
    child_path = fork_services.session_service.path_resolver.resolve_session_node(
        child.session_id
    )
    assert child_path.parent == source_path.parent
    child_checkpoints = [
        item
        async for item in fork_services.checkpointer.alist(
            build_checkpoint_config(child.session_id)
        )
    ]
    assert len(child_checkpoints) == 2
    assert child_checkpoints[0].checkpoint["id"] != "checkpoint-prefix-2"
    assert child_checkpoints[0].checkpoint["channel_values"]["scratchpad"] == {
        "step": 2
    }

    await fork_services.session_service.delete(source.session_id)
    restored = await fork_services.checkpointer.aget_tuple(
        build_checkpoint_config(child.session_id)
    )
    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == [
        "第一轮",
        "第二轮输出",
    ]


@pytest.mark.asyncio
async def test_full_rollout_copy_clones_single_rollout_as_independent_storage(
    fork_services: ForkServices,
) -> None:
    source = await fork_services.session_service.create(
        SessionCreateRequest(title="完整复制源会话")
    )
    checkpoint = {
        "v": 1,
        "id": "checkpoint-full-1",
        "ts": "2026-07-13T00:00:00+00:00",
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="原始内容",
                    id="u1",
                    response_metadata={"turn_id": "turn-1"},
                ),
                AIMessage(content="原始回答", id="a1"),
            ],
            "scratchpad": {"step": 1},
        },
        "channel_versions": {"messages": "1", "scratchpad": "1"},
        "versions_seen": {},
        "pending_sends": [],
        "updated_channels": ["messages", "scratchpad"],
    }
    await fork_services.checkpointer.aput(
        build_checkpoint_config(source.session_id),
        checkpoint,
        {"source": "test", "step": 1, "parents": {}},
        {"messages": "1", "scratchpad": "1"},
    )
    fork_services.checkpointer.finalize_turn(
        session_id=source.session_id,
        turn_id="turn-1",
        final_message_id="a1",
    )
    fork_services.checkpointer.rewind(
        build_checkpoint_config(source.session_id),
        checkpoint_id="checkpoint-full-1",
        source_anchor="u1",
    )

    child = await fork_services.fork_service.fork(
        source.session_id,
        mode="full_rollout_copy",
    )

    source_path = fork_services.session_service.path_resolver.resolve_session_node(
        source.session_id
    )
    child_path = fork_services.session_service.path_resolver.resolve_session_node(
        child.session_id
    )
    assert child_path != source_path
    assert (source_path / "rollout" / "rollout.jsonl").is_file()
    assert (source_path / "rollout" / "index.sqlite").is_file()
    assert (child_path / "rollout" / "rollout.jsonl").is_file()
    assert (child_path / "rollout" / "index.sqlite").is_file()
    assert not list(source_path.glob("rollout/segment-*.jsonl"))
    assert not list(child_path.glob("rollout/segment-*.jsonl"))
    assert fork_services.checkpointer.rollout_id(source.session_id) != (
        fork_services.checkpointer.rollout_id(child.session_id)
    )

    await fork_services.session_service.delete(source.session_id)
    restored = await fork_services.checkpointer.aget_tuple(
        build_checkpoint_config(child.session_id)
    )
    assert restored is not None
    assert restored.checkpoint["channel_values"]["messages"][0].content == "原始内容"
