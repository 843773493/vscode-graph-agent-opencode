from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.core.rollout_storage import RolloutStorage
from app.schemas.public_v2.session import SessionCreateRequest
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore


@dataclass(frozen=True, slots=True)
class ForkIntegrationContext:
    sessions: SessionService
    saver: RolloutCheckpointSaver
    forks: SessionContextForkService


@pytest.fixture
def fork_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ForkIntegrationContext:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    saver = RolloutCheckpointSaver(sessions_dir)
    sessions = SessionService(
        config_service=ConfigService(workspace_root=tmp_path),
        trace_event_store=TraceEventStore(sessions_dir=sessions_dir),
        fork_relationship_checker=saver,
    )
    return ForkIntegrationContext(
        sessions=sessions,
        saver=saver,
        forks=SessionContextForkService(session_service=sessions, checkpointer=saver),
    )


def _checkpoint(
    checkpoint_id: str,
    messages: list[object],
    *,
    pending_sends: list[object] | None = None,
) -> dict[str, object]:
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-08-15T00:00:00+00:00",
        "channel_values": {"messages": messages, "counter": len(messages)},
        "channel_versions": {
            "messages": str(len(messages)),
            "counter": str(len(messages)),
        },
        "versions_seen": {},
        "pending_sends": pending_sends or [],
        "updated_channels": ["messages", "counter"],
    }


async def _seed_source(context: ForkIntegrationContext):
    source = await context.sessions.create(
        SessionCreateRequest(title="Fork 集成源会话")
    )
    first_messages = [
        HumanMessage(
            content="问题一",
            id="u1",
            response_metadata={"turn_id": "turn-1"},
        )
    ]
    first_config = await context.saver.aput(
        build_checkpoint_config(source.session_id),
        _checkpoint("cp1", first_messages),
        {"source": "stub", "step": 1, "parents": {}},
        {"messages": "1", "counter": "1"},
    )
    second_messages = [*first_messages, AIMessage(content="回答一", id="a1")]
    second_config = await context.saver.aput(
        first_config,
        _checkpoint(
            "cp2",
            second_messages,
            pending_sends=[{"node": "pending-node", "attempt": 2}],
        ),
        {"source": "stub", "step": 2, "parents": {}},
        {"messages": "2", "counter": "2"},
    )
    context.saver.finalize_turn(
        session_id=source.session_id,
        turn_id="turn-1",
        final_message_id="a1",
    )
    await context.saver.aput_writes(
        second_config,
        [("scratchpad", {"pending": True})],
        "task-pending",
        "parent/child",
    )
    await context.saver.aput(
        second_config,
        _checkpoint(
            "cp3",
            [
                *second_messages,
                HumanMessage(
                    content="问题二",
                    id="u2",
                    response_metadata={"turn_id": "turn-2"},
                ),
            ],
        ),
        {"source": "stub", "step": 3, "parents": {}},
        {"messages": "3", "counter": "3"},
    )
    return source


def _fork_origins(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = context.sessions.path_resolver.resolve_session_node(session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT child_session_id, source_session_id, source_checkpoint_id, "
            "source_view_id, fork_mode, relationship FROM fork_origins"
        ).fetchall()


def _retention_refs(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = context.sessions.path_resolver.resolve_session_node(session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT reference_kind, reference_id, target_view_id, "
            "owner_session_id, status FROM retention_refs"
        ).fetchall()


def _pending_write_rows(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = context.sessions.path_resolver.resolve_session_node(session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT task_id, task_path, write_index, channel, status "
            "FROM pending_writes ORDER BY task_path, write_index"
        ).fetchall()


def _fork_materialization_rows(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = context.sessions.path_resolver.resolve_session_node(session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT materialization_id, fork_id, status, copied_message_count "
            "FROM fork_materializations"
        ).fetchall()


@pytest.mark.asyncio
async def test_all_fork_modes_materialize_independent_rollouts(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    context_child = await fork_context.forks.fork(
        source.session_id,
        mode="context_fork",
        checkpoint_id="cp2",
        anchor="u1",
    )
    prefix_child = await fork_context.forks.fork(
        source.session_id,
        mode="history_prefix_fork",
        checkpoint_id="cp2",
    )
    # 模拟源 rollout 自身已经是一个 fork，验证完整复制不会继承这条旧关系。
    fork_context.saver.record_fork_origin(
        target_thread_id=source.session_id,
        source_session_id="ses_old_source",
        source_checkpoint_id="old-checkpoint",
        source_view_id="old-view",
        fork_mode="context_fork",
    )
    full_child = await fork_context.forks.fork(
        source.session_id,
        mode="full_rollout_copy",
        checkpoint_id="cp2",
    )

    context_tuple = await fork_context.saver.aget_tuple(
        build_checkpoint_config(context_child.session_id)
    )
    prefix_tuple = await fork_context.saver.aget_tuple(
        build_checkpoint_config(prefix_child.session_id)
    )
    full_tuple = await fork_context.saver.aget_tuple(
        build_checkpoint_config(full_child.session_id)
    )
    assert context_tuple is not None
    assert prefix_tuple is not None
    assert full_tuple is not None
    origins = {
        child.session_id: _fork_origins(fork_context, child.session_id)
        for child in (context_child, prefix_child, full_child)
    }
    assert all(len(rows) == 1 for rows in origins.values())
    assert origins[context_child.session_id][0][1:] == (
        source.session_id,
        "cp2",
        origins[context_child.session_id][0][3],
        "context_fork",
        "detached",
    )
    assert origins[prefix_child.session_id][0][1:] == (
        source.session_id,
        "cp2",
        origins[prefix_child.session_id][0][3],
        "history_prefix_fork",
        "detached",
    )
    assert origins[full_child.session_id][0][1:] == (
        source.session_id,
        "cp2",
        origins[full_child.session_id][0][3],
        "full_rollout_copy",
        "detached",
    )
    assert all(rows[0][3] is not None for rows in origins.values())
    assert [
        message.content
        for message in context_tuple.checkpoint["channel_values"]["messages"]
    ] == [
        "问题一",
        "回答一",
    ]
    assert [
        message.content
        for message in prefix_tuple.checkpoint["channel_values"]["messages"]
    ] == [
        "问题一",
        "回答一",
    ]
    assert [
        message.content
        for message in full_tuple.checkpoint["channel_values"]["messages"]
    ] == [
        "问题一",
        "回答一",
        "问题二",
    ]
    assert context_tuple.checkpoint["pending_sends"] == [
        {"node": "pending-node", "attempt": 2}
    ]
    assert context_tuple.pending_writes == [
        ("task-pending", "scratchpad", {"pending": True})
    ]
    assert _pending_write_rows(fork_context, context_child.session_id) == [
        ("task-pending", "parent/child", 0, "scratchpad", "pending")
    ]
    prefix_pending = [
        item
        for item in [
            item
            async for item in fork_context.saver.alist(
                build_checkpoint_config(prefix_child.session_id)
            )
        ]
        if item.checkpoint["id"] != ""
    ]
    assert any(
        item.checkpoint["pending_sends"] == [{"node": "pending-node", "attempt": 2}]
        and item.pending_writes == [("task-pending", "scratchpad", {"pending": True})]
        for item in prefix_pending
    )
    assert _pending_write_rows(fork_context, prefix_child.session_id) == [
        ("task-pending", "parent/child", 0, "scratchpad", "pending")
    ]
    full_pending = await fork_context.saver.aget_tuple(
        build_checkpoint_config(full_child.session_id, checkpoint_id="cp2")
    )
    assert full_pending is not None
    assert full_pending.checkpoint["pending_sends"] == [
        {"node": "pending-node", "attempt": 2}
    ]
    assert full_pending.pending_writes == [
        ("task-pending", "scratchpad", {"pending": True})
    ]
    assert _pending_write_rows(fork_context, full_child.session_id) == [
        ("task-pending", "parent/child", 0, "scratchpad", "pending")
    ]
    for child in (context_child, prefix_child, full_child):
        assert _fork_materialization_rows(fork_context, child.session_id)[0][2] == (
            "committed"
        )
        child_root = fork_context.sessions.path_resolver.resolve_session_node(
            child.session_id
        )
        with sqlite3.connect(child_root / "rollout" / "index.sqlite") as connection:
            assert connection.execute(
                "SELECT status FROM turns WHERE turn_id = 'turn-1'"
            ).fetchone() == ("completed",)
            assert connection.execute(
                "SELECT final_message_id FROM turns WHERE turn_id = 'turn-1'"
            ).fetchone() == ("a1",)
            turn_two_status = connection.execute(
                "SELECT status FROM turns WHERE turn_id = 'turn-2'"
            ).fetchone()
            assert turn_two_status is None or turn_two_status == ("cancelled",)
    child_root = (
        fork_context.sessions.path_resolver.resolve_session_node(
            context_child.session_id
        )
        / "rollout"
    )
    assert (child_root / "rollout.jsonl").is_file()
    assert (child_root / "index.sqlite").is_file()
    assert not list(child_root.glob("segment-*.jsonl"))

    await fork_context.sessions.delete(source.session_id)
    for child in (context_child, prefix_child, full_child):
        assert (
            await fork_context.saver.aget_tuple(
                build_checkpoint_config(child.session_id)
            )
            is not None
        )


@pytest.mark.asyncio
async def test_interrupted_fork_materialization_is_rolled_back_on_next_open(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await fork_context.sessions.create(
        SessionCreateRequest(title="中断 fork 源会话")
    )
    target = await fork_context.sessions.create(
        SessionCreateRequest(title="中断 fork 目标会话")
    )
    materialization_id, _fork_id = fork_context.saver._storage.begin_fork_materialization(
        target_session_id=target.session_id,
        source_session_id=source.session_id,
        source_checkpoint_id=None,
        source_view_id=None,
        fork_mode="context_fork",
        relationship="detached",
    )

    # 新的 RolloutStorage 实例模拟进程重启：内存中的 active 标记不存在，
    # initialize 必须按 journal 清理目标，而不是把半成品当成空 checkpoint。
    restarted_storage = RolloutStorage(
        fork_context.sessions.path_resolver.sessions_root
    )
    restarted_storage.initialize(target.session_id)
    target_root = fork_context.sessions.path_resolver.resolve_session_node(
        target.session_id
    )
    with sqlite3.connect(target_root / "rollout" / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT status FROM fork_materializations WHERE materialization_id = ?",
            (materialization_id,),
        ).fetchone() == ("aborted",)
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
    assert (target_root / "rollout" / "rollout.jsonl").read_bytes() == b""


@pytest.mark.asyncio
async def test_context_fork_anchor_selects_completed_target_turn(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)

    child = await fork_context.forks.fork(source.session_id)
    restored = await fork_context.saver.aget_tuple(
        build_checkpoint_config(child.session_id)
    )

    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == [
        "问题一",
        "回答一",
    ]


@pytest.mark.asyncio
async def test_default_fork_does_not_materialize_running_tail(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)

    child = await fork_context.forks.fork(source.session_id)
    restored = await fork_context.saver.aget_tuple(
        build_checkpoint_config(child.session_id)
    )

    assert restored is not None
    assert [
        message.content
        for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一"]
    assert _fork_origins(fork_context, child.session_id)[0][2] == "cp2"
    child_root = fork_context.sessions.path_resolver.resolve_session_node(
        child.session_id
    )
    with sqlite3.connect(child_root / "rollout" / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT 1 FROM turns WHERE turn_id = 'turn-2'"
        ).fetchone() is None


@pytest.mark.asyncio
async def test_fork_rejects_explicit_running_turn(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)

    with pytest.raises(ValueError, match="运行中的 Turn 不支持 fork"):
        await fork_context.forks.fork(source.session_id, turn_id="turn-2")


@pytest.mark.asyncio
async def test_context_fork_turn_id_resolves_latest_active_lineage_view(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)

    child = await fork_context.forks.fork(source.session_id, turn_id="turn-1")
    restored = await fork_context.saver.aget_tuple(
        build_checkpoint_config(child.session_id)
    )

    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一"]
    assert _fork_origins(fork_context, child.session_id)[0][3] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["history_prefix_fork", "full_rollout_copy"])
async def test_non_default_fork_modes_honor_turn_id_boundary(
    fork_context: ForkIntegrationContext,
    mode: str,
) -> None:
    source = await _seed_source(fork_context)

    child = await fork_context.forks.fork(
        source.session_id,
        mode=mode,  # type: ignore[arg-type]
        turn_id="turn-1",
    )
    restored = await fork_context.saver.aget_tuple(
        build_checkpoint_config(child.session_id)
    )

    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一"]


@pytest.mark.asyncio
async def test_pinned_fork_keeps_source_deletion_blocked(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    child = await fork_context.forks.fork(
        source.session_id,
        pinned=True,
        place_under_source=True,
    )
    with pytest.raises(RuntimeError, match=child.session_id):
        await fork_context.sessions.delete(source.session_id)


@pytest.mark.asyncio
async def test_pinned_fork_retention_released_when_child_is_deleted(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context)
    child = await fork_context.forks.fork(
        source.session_id,
        pinned=True,
        place_under_source=True,
    )

    active_refs = _retention_refs(fork_context, source.session_id)
    assert len(active_refs) == 1
    assert active_refs[0][0] == "fork"
    assert active_refs[0][2] is not None
    assert active_refs[0][3] == child.session_id
    assert active_refs[0][4] == "active"

    await fork_context.sessions.delete(child.session_id)

    released_refs = _retention_refs(fork_context, source.session_id)
    assert released_refs[0][4] == "released"
    await fork_context.sessions.delete(source.session_id)
