from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import get_session_creation_service, get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.schemas.internal_v2.session import SessionCreateRequest
from app.services.business.session_context_fork_service import (
    SessionContextForkService,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
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
    # R17：workspace_id 与 resolver 同源（identity API）；catalog 模式下
    # register 校验 manifest workspace_id 与分配一致，不再硬编码假 UUID。
    from app.core.workspace_identity import load_or_create_workspace_id

    session_service = SessionService(
        config_service=ConfigService(workspace_root=tmp_path),
        trace_event_store=TraceEventStore(sessions_dir=sessions_dir),
        workspace_id=load_or_create_workspace_id(tmp_path),
        path_resolver=get_session_path_resolver(sessions_dir),
        creation_service=get_session_creation_service(sessions_dir),
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


@pytest.fixture
def write_checkpoint(
    fork_services: ForkServices,
) -> Callable[..., Awaitable[RunnableConfig]]:
    """夹具使用真实 acceptance/admission 与 SessionThread owner 写入 checkpoint。"""

    async def write(
        session_id: str,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        versions: ChannelVersions,
        config: RunnableConfig | None = None,
    ) -> RunnableConfig:
        saver = fork_services.checkpointer
        owner = saver.resolve_main_thread_owner(session_id)
        user = next(
            message for message in checkpoint["channel_values"]["messages"]
            if isinstance(message, HumanMessage)
        )
        turn_id = user.response_metadata.get("turn_id", "turn-1")
        accepted = saver.accept_turn(
            session_id, thread_id=owner.thread_id,
            accepted_ingress_id=f"ingress-fork-fixture:{turn_id}",
            acceptance_idempotency_key=f"accept-fork-fixture:{turn_id}",
            job_id=f"job-fork-fixture:{turn_id}",
            admission_idempotency_key=f"admit-fork-fixture:{turn_id}",
            payload=user.content, turn_id=turn_id,
            root_item_id=f"item-{user.id}",
            initial_execution_id=f"execution-fork-fixture:{turn_id}",
        )
        admission = saver.execution_admission(
            session_id, thread_id=owner.thread_id, execution_id=accepted["execution_id"],
        )
        with (
            session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
            execution_admission_scope(admission, langgraph_thread_id=owner.thread_id),
        ):
            return await saver.aput(
                build_checkpoint_config(owner.thread_id) if config is None else config,
                checkpoint, metadata, versions,
            )

    return write


@pytest.fixture
def read_checkpoint(
    fork_services: ForkServices,
) -> Callable[[str], Awaitable[CheckpointTuple | None]]:
    "按同一 SessionThread owner 读取并恢复 checkpoint，不以 Session ID 冒充 Thread。"

    async def read(session_id: str) -> CheckpointTuple | None:
        saver = fork_services.checkpointer
        owner = saver.resolve_main_thread_owner(session_id)
        with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
            return await saver.aget_tuple(build_checkpoint_config(owner.thread_id))

    return read


@pytest.mark.asyncio
async def test_fork_copies_agent_state_into_independent_child(
    fork_services: ForkServices,
    read_checkpoint: Callable[[str], Awaitable[CheckpointTuple | None]],
    write_checkpoint: Callable[..., Awaitable[RunnableConfig]],
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
        "channel_versions": {"messages": "1", "scratchpad": "1"},
        "versions_seen": {},
        "pending_sends": [],
        "updated_channels": ["messages", "scratchpad"],
    }
    await write_checkpoint(
        source.session_id,
        checkpoint,
        {"source": "loop", "step": 2, "parents": {}},
        {"messages": "1", "scratchpad": "1"},
    )
    fork_services.checkpointer.finalize_turn(
        session_id=source.session_id,
        thread_id=fork_services.checkpointer._storage.main_thread_id(source.session_id),
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

    source_tuple = await read_checkpoint(source.session_id)
    child_tuple = await read_checkpoint(child.session_id)
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
async def test_fork_empty_context_is_rejected_before_child_creation(
    fork_services: ForkServices,
) -> None:
    source = await fork_services.session_service.create(
        SessionCreateRequest(title="空上下文")
    )

    with pytest.raises(ValueError, match="fork_source_not_completed"):
        await fork_services.fork_service.fork(source.session_id)


@pytest.mark.asyncio
async def test_detached_fork_survives_parent_deletion(
    fork_services: ForkServices,
    read_checkpoint: Callable[[str], Awaitable[CheckpointTuple | None]],
    write_checkpoint: Callable[..., Awaitable[RunnableConfig]],
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
        "channel_versions": {"messages": "1", "scratchpad": "1"},
        "versions_seen": {},
        "pending_sends": [],
        "updated_channels": ["messages", "scratchpad"],
    }
    await write_checkpoint(
        source.session_id,
        checkpoint,
        {"source": "test", "step": 1, "parents": {}},
        {"messages": "1", "counter": "1"},
    )
    fork_services.checkpointer.finalize_turn(
        session_id=source.session_id,
        thread_id=fork_services.checkpointer._storage.main_thread_id(source.session_id),
        turn_id="turn-1",
        final_message_id="a1",
    )

    child = await fork_services.fork_service.fork(source.session_id)
    await fork_services.session_service.delete(source.session_id)

    restored = await read_checkpoint(child.session_id)
    assert restored is not None
    assert restored.checkpoint["channel_values"]["messages"][0].content == "独立上下文"


@pytest.mark.asyncio
async def test_pinned_fork_blocks_parent_deletion(
    fork_services: ForkServices,
    write_checkpoint: Callable[..., Awaitable[RunnableConfig]],
) -> None:
    source = await fork_services.session_service.create(
        SessionCreateRequest(title="固定源会话")
    )
    checkpoint = {
        "v": 1,
        "id": "checkpoint-pinned-source",
        "ts": "2026-07-13T00:00:00+00:00",
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="固定源上下文",
                    id="pinned-user-1",
                    response_metadata={"turn_id": "pinned-turn-1"},
                ),
                AIMessage(content="固定源回答", id="pinned-final-1"),
            ],
        },
        "channel_versions": {"messages": "1"},
        "versions_seen": {},
        "pending_sends": [],
        "updated_channels": ["messages"],
    }
    await write_checkpoint(
        source.session_id,
        checkpoint,
        {"source": "test", "step": 1, "parents": {}},
        {"messages": "1"},
    )
    fork_services.checkpointer.finalize_turn(
        session_id=source.session_id,
        thread_id=fork_services.checkpointer._storage.main_thread_id(source.session_id),
        turn_id="pinned-turn-1",
        final_message_id="pinned-final-1",
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
    read_checkpoint: Callable[[str], Awaitable[CheckpointTuple | None]],
    write_checkpoint: Callable[..., Awaitable[RunnableConfig]],
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
    first_config = await write_checkpoint(
        source.session_id,
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
    await write_checkpoint(
        source.session_id,
        second,
        {"source": "test", "step": 2, "parents": {}},
        {"messages": "2", "scratchpad": "2"},
        config=first_config,
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
    owner = fork_services.checkpointer.resolve_main_thread_owner(child.session_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        child_checkpoints = [
            item
            async for item in fork_services.checkpointer.alist(
                build_checkpoint_config(owner.thread_id)
            )
        ]
        earlier_checkpoints = [
            item
            async for item in fork_services.checkpointer.alist(
                build_checkpoint_config(owner.thread_id),
                before=child_checkpoints[0].config,
                limit=1,
            )
        ]
    assert len(child_checkpoints) == 2
    assert len(earlier_checkpoints) == 1
    assert earlier_checkpoints[0].checkpoint["id"] == child_checkpoints[1].checkpoint["id"]
    assert earlier_checkpoints[0].checkpoint["channel_values"]["scratchpad"] == {
        "step": 1
    }
    assert child_checkpoints[0].checkpoint["id"] != "checkpoint-prefix-2"
    assert child_checkpoints[0].checkpoint["channel_values"]["scratchpad"] == {
        "step": 2
    }

    await fork_services.session_service.delete(source.session_id)
    restored = await read_checkpoint(child.session_id)
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
    read_checkpoint: Callable[[str], Awaitable[CheckpointTuple | None]],
    write_checkpoint: Callable[..., Awaitable[RunnableConfig]],
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
    await write_checkpoint(
        source.session_id,
        checkpoint,
        {"source": "test", "step": 1, "parents": {}},
        {"messages": "1", "scratchpad": "1"},
    )
    fork_services.checkpointer.finalize_turn(
        session_id=source.session_id,
        thread_id=fork_services.checkpointer._storage.main_thread_id(source.session_id),
        turn_id="turn-1",
        final_message_id="a1",
    )
    owner = fork_services.checkpointer.resolve_main_thread_owner(source.session_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        fork_services.checkpointer.rewind(
            build_checkpoint_config(owner.thread_id),
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
    restored = await read_checkpoint(child.session_id)
    assert restored is not None
    assert restored.checkpoint["channel_values"]["messages"][0].content == "原始内容"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_marker", [False, True])
async def test_full_rollout_copy_accepts_empty_catalog_and_rejects_missing_marker(
    fork_services: ForkServices,
    read_checkpoint: Callable[[str], Awaitable[CheckpointTuple | None]],
    write_checkpoint: Callable[..., Awaitable[RunnableConfig]], missing_marker: bool,
) -> None:
    """fresh 空 catalog 合法；既有库缺 marker 则不能回退成无 activation 事实。"""

    from app.services.infrastructure.rollout_context.storage.resource_activation_store import (
        ResourceActivationStoreError,
    )

    source = await fork_services.session_service.create(
        SessionCreateRequest(title="无 activation schema 源会话")
    )
    checkpoint = {
        "v": 1,
        "id": "checkpoint-no-activation-1",
        "ts": "2026-07-13T00:00:00+00:00",
        "channel_values": {
            "messages": [
                HumanMessage(
                    content="无 activation 内容",
                    id="u1",
                    response_metadata={"turn_id": "turn-1"},
                ),
                AIMessage(content="无 activation 回答", id="a1"),
            ],
        },
        "channel_versions": {"messages": "1"},
        "versions_seen": {},
        "pending_sends": [],
        "updated_channels": ["messages"],
    }
    await write_checkpoint(
        source.session_id,
        checkpoint,
        {"source": "test", "step": 1, "parents": {}},
        {"messages": "1"},
    )
    fork_services.checkpointer.finalize_turn(
        session_id=source.session_id,
        thread_id=fork_services.checkpointer._storage.main_thread_id(source.session_id),
        turn_id="turn-1",
        final_message_id="a1",
    )

    source_index = fork_services.checkpointer._storage.index_path(source.session_id, "")
    with sqlite3.connect(source_index) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "context_plan_details" in tables
    assert "resource_activation_snapshots" in tables
    with sqlite3.connect(source_index) as connection:
        assert connection.execute("SELECT COUNT(*) FROM resource_activation_snapshots").fetchone() == (0,)
        if missing_marker:
            connection.execute("DROP TABLE resource_activation_schema_state")
            connection.commit()
    if missing_marker:
        with pytest.raises(ResourceActivationStoreError) as error:
            await fork_services.fork_service.fork(source.session_id, mode="full_rollout_copy")
        assert error.value.code == "resource-activation-schema-unavailable"
        return

    child = await fork_services.fork_service.fork(
        source.session_id,
        mode="full_rollout_copy",
    )

    child_index = fork_services.checkpointer._storage.index_path(child.session_id, "")
    assert child_index.is_file()
    restored = await read_checkpoint(child.session_id)
    assert restored is not None
    assert (
        restored.checkpoint["channel_values"]["messages"][0].content
        == "无 activation 内容"
    )
