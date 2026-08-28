from __future__ import annotations

import json
import multiprocessing
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.core.rollout_append_writer import RolloutAppendWriter
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.core.rollout_context_reader import RolloutContextReader
from app.core.rollout_storage import RolloutStorage
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader


def _checkpoint(
    checkpoint_id: str, messages: list[object], **channels: object
) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages, **channels}
    checkpoint["channel_versions"] = {
        name: str(index + 1) for index, name in enumerate(checkpoint["channel_values"])
    }
    checkpoint["updated_channels"] = list(checkpoint["channel_values"])
    return checkpoint


def _turn(turn_id: str, suffix: str) -> list[object]:
    user = HumanMessage(
        content=f"用户问题 {suffix}",
        id=f"user-{suffix}",
        response_metadata={"message_metadata": {"turn_id": turn_id, "job_id": turn_id}},
    )
    call = AIMessage(
        content=f"检查 {suffix}",
        id=f"call-message-{suffix}",
        tool_calls=[
            {"name": "read_file", "args": {"path": suffix}, "id": f"call-{suffix}"}
        ],
    )
    result = ToolMessage(
        content=f"工具结果 {suffix}",
        id=f"result-{suffix}",
        name="read_file",
        tool_call_id=f"call-{suffix}",
    )
    final = AIMessage(content=f"最终响应 {suffix}", id=f"final-{suffix}")
    return [user, call, result, final]


def test_rollout_preserves_langchain_invalid_tool_calls_field(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    invalid = AIMessage(
        content="需要重新生成工具参数",
        id="invalid-call-message",
        invalid_tool_calls=[
            {
                "type": "invalid_tool_call",
                "id": "call-invalid",
                "name": "read_file",
                "args": '{"path":',
                "error": "arguments 不是合法 JSON object",
            }
        ],
    )
    valid = AIMessage(content="普通响应", id="valid-message")
    saver = RolloutCheckpointSaver(sessions_dir)
    saver.put(
        build_checkpoint_config("session_1"),
        _checkpoint(
            "cp-invalid-tool-call",
            [HumanMessage(content="检查文件", id="user-1"), valid, invalid],
        ),
        {"source": "invalid-tool-call-field-test"},
        {"messages": "test"},
    )

    records = [
        json.loads(line)
        for line in (
            get_session_path_resolver(sessions_dir)
            .resolve_session_node("session_1")
            / "rollout"
            / "rollout.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assistant_data = [
        record["message"]["data"]
        for record in records
        if record["role"] == "assistant"
    ]
    assert assistant_data[0]["invalid_tool_calls"] == []
    assert assistant_data[1]["invalid_tool_calls"][0]["id"] == "call-invalid"

    restored = saver.get_tuple(build_checkpoint_config("session_1"))
    assert restored is not None
    restored_invalid = restored.checkpoint["channel_values"]["messages"][-1]
    assert isinstance(restored_invalid, AIMessage)
    assert restored_invalid.invalid_tool_calls[0]["id"] == "call-invalid"


def test_put_treats_unpersisted_initial_parent_as_root(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """首个真实写入不能把 LangGraph 的内存父 ID 落成孤儿引用。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)

    config = build_checkpoint_config(
        "session_1", checkpoint_id="unpersisted-initial-checkpoint"
    )
    saver.put(
        config,
        _checkpoint(
            "cp-root",
            [HumanMessage(content="首条消息", id="user-root")],
        ),
        {"source": "root-parent-test"},
        {"messages": "1"},
    )

    restored = saver.get_tuple(build_checkpoint_config("session_1"))
    assert restored is not None
    assert restored.parent_config is None
    root = get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT parent_checkpoint_id FROM checkpoints WHERE checkpoint_id = 'cp-root'"
        ).fetchone()[0] is None


def test_put_repairs_empty_message_channel_version(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "cp-empty-message-version"
    checkpoint["channel_values"] = {
        "messages": [HumanMessage(content="消息版本为空", id="user-empty-version")],
    }
    checkpoint["channel_versions"] = {"messages": None}
    checkpoint["updated_channels"] = ["messages"]

    saver.put(
        build_checkpoint_config("session_1"),
        checkpoint,
        {"source": "empty-message-version"},
        {},
    )

    restored = saver.get_tuple(build_checkpoint_config("session_1"))
    assert restored is not None
    assert restored.checkpoint["channel_versions"]["messages"].startswith(
        "checkpoint:"
    )


def test_checkpoint_namespace_queries_do_not_cross_match(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    namespace_a = build_checkpoint_config("session_1", checkpoint_ns="ns-a")
    namespace_b = build_checkpoint_config("session_1", checkpoint_ns="ns-b")

    saver.put(
        namespace_a,
        _checkpoint("cp-ns-a", [HumanMessage(content="A", id="user-a")]),
        {"source": "namespace-a"},
        {"messages": "a"},
    )
    saver.put(
        namespace_b,
        _checkpoint("cp-ns-b", [HumanMessage(content="B", id="user-b")]),
        {"source": "namespace-b"},
        {"messages": "b"},
    )

    restored_a = saver.get_tuple(namespace_a)
    restored_b = saver.get_tuple(namespace_b)
    assert restored_a is not None
    assert restored_b is not None
    assert restored_a.checkpoint["id"] == "cp-ns-a"
    assert restored_b.checkpoint["id"] == "cp-ns-b"
    assert [item.checkpoint["id"] for item in saver.list(namespace_a)] == ["cp-ns-a"]
    assert [item.checkpoint["id"] for item in saver.list(namespace_b)] == ["cp-ns-b"]


def _append_checkpoint_in_child(sessions_dir: str) -> None:
    saver = RolloutCheckpointSaver(sessions_dir)
    saver.put(
        build_checkpoint_config("session_1"),
        _checkpoint("cp-child", [HumanMessage(content="子进程写入", id="child-u")]),
        {"source": "cross-process-lock"},
        {"messages": "child"},
    )


def test_read_snapshot_keeps_sqlite_and_jsonl_watermark_consistent_across_processes(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    saver.put(
        build_checkpoint_config("session_1"),
        _checkpoint("cp-parent", [HumanMessage(content="父进程写入", id="parent-u")]),
        {"source": "snapshot-test"},
        {"messages": "parent"},
    )
    storage = RolloutStorage(sessions_dir)
    snapshot = storage.open_read_snapshot("session_1")
    child = multiprocessing.get_context("spawn").Process(
        target=_append_checkpoint_in_child,
        args=(str(sessions_dir),),
    )
    child.start()
    try:
        time.sleep(0.25)
        assert child.is_alive(), "写进程未被 read snapshot 的跨文件锁阻塞"
        assert snapshot.manifest.latest_checkpoint_id == "cp-parent"
        assert (
            snapshot.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            == 1
        )
    finally:
        snapshot.close()
    child.join(timeout=10)
    assert child.exitcode == 0
    restored = RolloutCheckpointSaver(sessions_dir).get_tuple(
        build_checkpoint_config("session_1")
    )
    assert restored is not None
    assert restored.checkpoint["id"] == "cp-child"


def test_read_snapshot_does_not_touch_existing_rollout_files(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    storage = RolloutStorage(sessions_dir)
    storage.initialize("session_1")
    rollout_root = (
        get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
        / "rollout"
    )
    rollout_path = rollout_root / "rollout.jsonl"
    index_path = rollout_root / "index.sqlite"
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in (rollout_path, index_path)
    }
    time.sleep(0.01)
    snapshot = storage.open_read_snapshot("session_1")
    try:
        assert snapshot.connection.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        snapshot.close()
    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in (rollout_path, index_path)
    }
    assert after == before


def test_delete_legacy_rollout_does_not_initialize_removed_layout(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_legacy")
    rollout_root = (
        get_session_path_resolver(sessions_dir).resolve_session_node("session_legacy")
        / "rollout"
    )
    rollout_root.mkdir(parents=True)
    (rollout_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    (rollout_root / "segment-001.jsonl").write_text("{}\n", encoding="utf-8")
    (rollout_root / "index.sqlite").touch()

    storage = RolloutStorage(sessions_dir)

    assert storage.pinned_fork_children("session_legacy") == ()
    storage.release_fork_retentions("session_legacy")
    storage.delete_thread("session_legacy")

    assert not rollout_root.exists()


def test_validate_index_uses_read_only_snapshot_for_maintenance_check(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    storage = RolloutStorage(sessions_dir)
    storage.initialize("session_1")

    snapshot = storage.validate_index("session_1")
    try:
        assert snapshot.connection.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        snapshot.close()


def test_history_page_uses_one_snapshot_and_sqlite_keyset_window(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    first = _turn("turn-1", "001")
    config = saver.put(
        build_checkpoint_config("session_1"),
        _checkpoint("cp-1", first),
        {"source": "keyset-test"},
        {"messages": "1"},
    )
    saver.put(
        config,
        _checkpoint("cp-2", [*first, *_turn("turn-2", "002")]),
        {"source": "keyset-test"},
        {"messages": "2"},
    )
    reader = RolloutHistoryReader(
        RolloutContextReader(RolloutStorage(sessions_dir))
    )
    original_open_snapshot = reader._context_reader.open_snapshot
    opened = 0

    def counted_open_snapshot(*args: object, **kwargs: object):
        nonlocal opened
        opened += 1
        return original_open_snapshot(*args, **kwargs)

    monkeypatch.setattr(
        reader._context_reader,
        "open_snapshot",
        counted_open_snapshot,
    )
    page = reader.load(
        "session_1",
        TurnHistoryLoadRequest(direction="tail", turns=1),
    )
    assert opened == 1
    assert [item.ordinal for item in page.items] == [2]
    assert page.has_more is True


def test_history_index_failure_closes_read_snapshot(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    storage = RolloutStorage(sessions_dir)
    storage.initialize("session_1")
    reader = RolloutHistoryReader(RolloutContextReader(storage))
    snapshot = storage.open_read_snapshot("session_1")
    monkeypatch.setattr(
        reader._context_reader,
        "open_snapshot",
        lambda _session_id: snapshot,
    )

    def fail_after_open(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("模拟索引解析失败")

    monkeypatch.setattr(reader, "_read_indexed_history_snapshot", fail_after_open)

    with pytest.raises(RuntimeError, match="模拟索引解析失败"):
        reader._read_indexed_history("session_1")
    assert snapshot.closed is True


def test_checkpoint_envelope_and_channels_are_authoritative_sqlite(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    messages = _turn("turn-1", "001")
    config = saver.put(
        build_checkpoint_config("session_1"),
        _checkpoint("cp-1", messages, counter=3, task_state=None),
        {"source": "unit", "step": 1},
        {"messages": "1", "counter": "1", "task_state": "1"},
    )
    saver.finalize_turn(
        session_id="session_1",
        turn_id="turn-1",
        final_message_id="final-001",
    )

    restored = saver.get_tuple(config)
    assert restored is not None
    assert [
        message.id for message in restored.checkpoint["channel_values"]["messages"]
    ] == [message.id for message in messages]
    assert restored.checkpoint["channel_values"]["counter"] == 3
    assert restored.checkpoint["channel_values"]["task_state"] is None

    rollout_root = (
        get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
        / "rollout"
    )
    assert (rollout_root / "rollout.jsonl").is_file()
    assert (rollout_root / "index.sqlite").is_file()
    assert not list(rollout_root.glob("segment-*.jsonl"))
    lines = [
        json.loads(line)
        for line in (rollout_root / "rollout.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(lines) == 4
    assert {line["role"] for line in lines} == {"user", "assistant", "tool"}
    assert all("kind" not in line for line in lines)
    with sqlite3.connect(rollout_root / "index.sqlite") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM control_events").fetchone()[0] >= 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM checkpoint_channels").fetchone()[0]
            == 3
        )
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4


def test_failed_turn_status_survives_history_reload(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    messages = [
        HumanMessage(
            content="失败后仍然可以重试",
            id="failed-user",
            response_metadata={
                "message_metadata": {"turn_id": "failed-turn", "job_id": "failed-turn"}
            },
        )
    ]
    saver.put(
        build_checkpoint_config("session_1"),
        _checkpoint("cp-failed", messages),
        {"source": "failed-turn-test"},
        {"messages": "1"},
    )

    writer = RolloutAppendWriter(sessions_dir)
    assert writer.mark_turn_terminal_status(
        session_id="session_1",
        turn_id="failed-turn",
        status="failed",
    ) is True

    page = RolloutHistoryReader(
        RolloutContextReader(RolloutStorage(sessions_dir))
    ).load(
        "session_1",
        TurnHistoryLoadRequest(direction="tail", turns=1),
    )
    assert page.items[0].turn_id == "failed-turn"
    assert page.items[0].status.value == "failed"


def test_rewind_replay_uses_new_canonical_suffix_without_replacement_event(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    first = _turn("turn-1", "001")
    first_config = saver.put(
        build_checkpoint_config("session_1"),
        _checkpoint("cp-1", first),
        {"source": "unit"},
        {"messages": "1"},
    )
    saver.finalize_turn(
        session_id="session_1", turn_id="turn-1", final_message_id="final-001"
    )
    second_config = saver.put(
        first_config,
        _checkpoint("cp-2", [*first, *_turn("turn-2", "002")]),
        {"source": "unit"},
        {"messages": "2"},
    )
    saver.rewind(build_checkpoint_config("session_1"), checkpoint_id="cp-1")
    replay = [first[0], AIMessage(content="编辑后的响应", id="replay-a")]
    replay_config = saver.put(
        build_checkpoint_config("session_1", checkpoint_id="cp-1"),
        _checkpoint("cp-3", replay),
        {"source": "replay"},
        {"messages": "3"},
    )

    restored = saver.get_tuple(replay_config)
    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == [
        "用户问题 001",
        "编辑后的响应",
    ]
    assert second_config["configurable"]["checkpoint_id"] == "cp-2"
    root = (
        get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
        / "rollout"
    )
    assert len((root / "rollout.jsonl").read_text(encoding="utf-8").splitlines()) == 9
    with sqlite3.connect(root / "index.sqlite") as connection:
        kinds = {
            row[0]
            for row in connection.execute("SELECT control_kind FROM control_events")
        }
        assert "rewind" in kinds
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'records'"
            ).fetchone()[0]
            == 0
        )


def test_pending_writes_and_all_checkpoint_channels_round_trip(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    config = saver.put(
        build_checkpoint_config("session_1"),
        _checkpoint(
            "cp-1", [HumanMessage(content="状态", id="u1")], counter=0, optional=None
        ),
        {"source": "unit"},
        {"messages": "1", "counter": "1", "optional": "1"},
    )
    saver.put_writes(config, [("counter", 1)], "task-1", "node-a")
    restored = saver.get_tuple(config)
    assert restored is not None
    assert restored.checkpoint["channel_values"]["counter"] == 0
    assert restored.checkpoint["channel_values"]["optional"] is None
    assert restored.pending_writes == [("task-1", "counter", 1)]


def test_uncommitted_jsonl_tail_is_truncated_but_sqlite_loss_is_explicit_failure(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    saver.put(
        build_checkpoint_config("session_1"),
        _checkpoint("cp-1", [HumanMessage(content="已提交", id="u1")]),
        {"source": "unit"},
        {"messages": "1"},
    )
    root = (
        get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
        / "rollout"
    )
    path = root / "rollout.jsonl"
    committed_size = path.stat().st_size
    with path.open("ab") as stream:
        stream.write(
            b'{"sequence":999,"message_id":"tail","turn_id":"t","role":"user","message":{}}\n'
        )
    assert path.stat().st_size > committed_size
    RolloutStorage(sessions_dir).initialize("session_1")
    assert path.stat().st_size == committed_size

    (root / "index.sqlite").write_bytes(b"not sqlite")
    with pytest.raises((sqlite3.DatabaseError, RuntimeError)):
        RolloutCheckpointSaver(sessions_dir).get_tuple(
            build_checkpoint_config("session_1")
        )


def test_half_line_and_fsync_failure_never_become_committed_messages(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    path = (
        get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
        / "rollout"
        / "rollout.jsonl"
    )

    def fail_fsync(_stream: object) -> None:
        raise OSError("fsync injected failure")

    monkeypatch.setattr("app.core.rollout_storage.os.fsync", fail_fsync)
    with pytest.raises(OSError, match="fsync"):
        saver.put(
            build_checkpoint_config("session_1"),
            _checkpoint("cp-fsync", [HumanMessage(content="未提交", id="u-fsync")]),
            {"source": "fsync-test"},
            {"messages": "1"},
        )
    assert path.stat().st_size > 0
    monkeypatch.setattr("app.core.rollout_storage.os.fsync", lambda _stream: None)
    RolloutStorage(sessions_dir).initialize("session_1")
    assert path.read_bytes() == b""
    path.write_bytes(b'{"sequence":1,"message_id":"half"')
    RolloutStorage(sessions_dir).initialize("session_1")
    assert path.read_bytes() == b""


def test_sqlite_commit_window_is_retryable_without_duplicate_jsonl(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    config = build_checkpoint_config("session_1")
    checkpoint = _checkpoint(
        "cp-commit-window",
        [HumanMessage(content="已提交但调用方崩溃", id="u-commit-window")],
    )
    storage = saver._storage
    original_commit = storage._commit_connection

    def commit_then_crash(connection: sqlite3.Connection) -> None:
        original_commit(connection)
        raise OSError("调用方在 SQLite commit 后崩溃")

    monkeypatch.setattr(storage, "_commit_connection", commit_then_crash)
    with pytest.raises(OSError, match="SQLite commit"):
        saver.put(config, checkpoint, {"source": "commit-window"}, {"messages": "1"})

    rollout_path = (
        get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
        / "rollout"
        / "rollout.jsonl"
    )
    committed_bytes = rollout_path.read_bytes()
    assert len(committed_bytes.splitlines()) == 1

    monkeypatch.setattr(storage, "_commit_connection", original_commit)
    retry_config = saver.put(
        config,
        checkpoint,
        {"source": "commit-window"},
        {"messages": "1"},
    )
    assert retry_config["configurable"]["checkpoint_id"] == "cp-commit-window"
    assert rollout_path.read_bytes() == committed_bytes
    assert saver.get_tuple(retry_config) is not None


def test_application_container_exposes_one_checkpoint_entrypoint(
    tmp_path: Path,
) -> None:
    from app.container import build_app_container

    container = build_app_container(workspace_root=tmp_path / "workspace")
    checkpointer = container.message_service._checkpointer
    assert isinstance(checkpointer, RolloutCheckpointSaver)
    runtime = container.rollout_checkpoint_runtime
    assert runtime.saver is checkpointer
    assert runtime.storage is checkpointer._storage
    assert runtime.append_writer is checkpointer._writer
    assert runtime.context_reader is checkpointer._context_reader
    assert runtime.history_reader is checkpointer._history_reader
    assert checkpointer is container.session_context_fork_service._checkpointer
    assert checkpointer is container.checkpointer
    assert not hasattr(container, "rollout_append_writer")
    assert checkpointer is container.session_turn_replay_service._checkpointer
    assert (
        checkpointer
        is container.context_compaction_service._checkpoint_store._checkpointer
    )
    assert (
        checkpointer
        is container.agent_execution_service._dependency_provider.get_checkpointer()
    )
    assert (
        checkpointer._storage
        is container.session_turn_history_service._checkpointer._history_reader._context_reader._storage
    )
    assert checkpointer._storage is checkpointer._writer._storage
    assert checkpointer._storage is checkpointer._context_reader._storage
    assert (
        checkpointer._storage
        is checkpointer._history_reader._context_reader._storage
    )
    other_container = build_app_container(workspace_root=tmp_path / "other-workspace")
    assert (
        checkpointer._storage
        is not other_container.message_service._checkpointer._storage
    )
    assert (
        container.session_subagent_service._session_orchestrator
        is container.session_orchestrator
    )
    assert (
        container.terminal_steering_service._session_orchestrator
        is container.session_orchestrator
    )


def test_concurrent_checkpoint_appends_do_not_interleave_jsonl(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)

    def append(index: int) -> None:
        saver.put(
            build_checkpoint_config("session_1"),
            _checkpoint(
                f"cp-concurrent-{index}",
                [HumanMessage(content=f"并发 {index}", id=f"u-concurrent-{index}")],
            ),
            {"source": "concurrency-test", "step": index},
            {"messages": str(index)},
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(append, range(4)))

    root = get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
    records = [
        json.loads(line)
        for line in (root / "rollout" / "rollout.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == 4
    assert {record["message_id"] for record in records} == {
        f"u-concurrent-{index}" for index in range(4)
    }


def test_repeating_same_checkpoint_is_idempotent_but_conflicting_payload_fails(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    config = build_checkpoint_config("session_1")
    checkpoint = _checkpoint(
        "cp-idempotent",
        [HumanMessage(content="只写一次", id="u-idempotent")],
    )
    metadata = {"source": "idempotency"}
    versions = {"messages": "1"}
    first_config = saver.put(config, checkpoint, metadata, versions)
    root = get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
    jsonl_path = root / "rollout" / "rollout.jsonl"
    original = jsonl_path.read_bytes()

    assert saver.put(config, checkpoint, metadata, versions) == first_config
    assert jsonl_path.read_bytes() == original
    with pytest.raises(ValueError, match="内容不一致"):
        saver.put(
            config,
            _checkpoint(
                "cp-idempotent",
                [HumanMessage(content="冲突", id="u-idempotent")],
            ),
            metadata,
            versions,
        )


def test_rollout_schema_exposes_all_authoritative_tables_and_core_constraints(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    storage = RolloutStorage(sessions_dir)
    storage.initialize("session_1")
    root = get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
    expected_columns = {
        "database_meta": {
            "schema_version",
            "message_format_version",
            "committed_jsonl_offset",
        },
        "schema_migrations": {
            "from_version",
            "to_version",
            "migration_checksum",
            "status",
        },
        "storage_commits": {
            "transaction_id",
            "jsonl_start_offset",
            "jsonl_end_offset",
            "status",
        },
        "control_events": {
            "control_sequence",
            "control_kind",
            "payload_json",
            "event_hash",
        },
        "branches": {"branch_id", "branch_kind", "head_view_id", "head_checkpoint_id"},
        "context_views": {
            "view_id",
            "parent_view_id",
            "view_kind",
            "head_message_sequence",
        },
        "context_view_ranges": {
            "view_id",
            "range_index",
            "source_kind",
            "start_message_sequence",
        },
        "context_view_jumps": {
            "view_id",
            "jump_level",
            "ancestor_view_id",
            "ancestor_depth",
        },
        "messages": {
            "message_sequence",
            "message_id",
            "jsonl_offset",
            "jsonl_length",
            "content_hash",
        },
        "message_projections": {"message_sequence", "visible_text", "has_tool_calls"},
        "turns": {
            "turn_id",
            "turn_ordinal",
            "user_message_sequence",
            "final_message_sequence",
        },
        "context_view_turns": {"view_id", "turn_id", "logical_turn_ordinal"},
        "tool_calls": {
            "tool_call_id",
            "assistant_message_sequence",
            "result_message_sequence",
        },
        "reasoning_blocks": {
            "message_sequence",
            "content_block_index",
            "item_index",
            "carrier_type",
            "provider_id",
        },
        "checkpoints": {
            "checkpoint_id",
            "checkpoint_json",
            "versions_seen_blob",
            "pending_sends_blob",
        },
        "checkpoint_channels": {
            "checkpoint_id",
            "channel_name",
            "storage_kind",
            "value_state",
        },
        "pending_writes": {"checkpoint_id", "task_id", "task_path", "write_index"},
        "fork_origins": {"fork_id", "source_session_id", "fork_mode", "relationship"},
        "retention_refs": {
            "retention_id",
            "reference_kind",
            "target_view_id",
            "status",
        },
        "fork_materializations": {
            "materialization_id",
            "fork_id",
            "target_session_id",
            "status",
            "rollback_jsonl_offset",
            "target_committed_at",
        },
    }
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert set(expected_columns) <= tables
        for table, columns in expected_columns.items():
            actual = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            assert columns <= actual, table
        schema_sql = " ".join(
            row[1]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            )
            if row[1]
        )
        assert "CHECK(singleton_id = 1)" in schema_sql
        assert "CHECK(role IN ('user','assistant','tool'))" in schema_sql


def test_sqlite_backup_restores_authoritative_checkpoint_state(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    saver = RolloutCheckpointSaver(sessions_dir)
    config = saver.put(
        build_checkpoint_config("session_1"),
        _checkpoint("cp-1", [HumanMessage(content="可恢复", id="u1")], counter=1),
        {"source": "backup-test"},
        {"messages": "1", "counter": "1"},
    )
    storage = RolloutStorage(sessions_dir)
    backup = storage.backup_index("session_1", destination=tmp_path / "index.bak")
    index_path = storage.index_path("session_1")
    index_path.write_bytes(b"corrupted")

    restored_snapshot = storage.restore_index_backup("session_1", backup)
    try:
        assert restored_snapshot.manifest.latest_checkpoint_id == "cp-1"
    finally:
        restored_snapshot.close()
    restored = RolloutCheckpointSaver(sessions_dir).get_tuple(config)
    assert restored is not None
    assert restored.checkpoint["channel_values"]["counter"] == 1


def test_schema_migration_checksum_and_completion_are_authoritative(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    storage = RolloutStorage(sessions_dir)
    storage.initialize("session_1")
    root = get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        connection.execute(
            "UPDATE schema_migrations SET migration_checksum = 'broken' WHERE to_version = 1"
        )
    with pytest.raises(RuntimeError, match="checksum"):
        storage.initialize("session_1")


def test_schema_migration_runs_transactionally_and_keeps_backup(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    storage = RolloutStorage(sessions_dir)
    storage.initialize("session_1")
    monkeypatch.setattr("app.core.rollout_storage.ROLLOUT_SCHEMA_VERSION", 2)

    snapshot = storage.migrate_schema(
        "session_1",
        to_version=2,
        migration_name="add_migration_probe_index",
        migration_sql="CREATE INDEX migration_probe ON messages(message_id)",
    )
    try:
        assert snapshot.manifest.rollout_id.startswith("rollout-")
    finally:
        snapshot.close()
    root = get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT schema_version FROM database_meta WHERE singleton_id = 1"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT status FROM schema_migrations WHERE to_version = 2"
            ).fetchone()[0]
            == "completed"
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'migration_probe'"
            ).fetchone()[0]
            == "migration_probe"
        )
    assert list(root.joinpath("rollout").glob("index.sqlite.migration-*.backup"))


def test_failed_schema_migration_restores_backup_and_requires_recovery(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, "session_1")
    storage = RolloutStorage(sessions_dir)
    storage.initialize("session_1")
    monkeypatch.setattr("app.core.rollout_storage.ROLLOUT_SCHEMA_VERSION", 2)

    with pytest.raises(sqlite3.OperationalError):
        storage.migrate_schema(
            "session_1",
            to_version=2,
            migration_name="broken_migration",
            migration_sql="ALTER TABLE table_that_does_not_exist ADD COLUMN value TEXT",
        )

    root = get_session_path_resolver(sessions_dir).resolve_session_node("session_1")
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT database_state FROM database_meta WHERE singleton_id = 1"
            ).fetchone()[0]
            == "recovery_required"
        )
        assert (
            connection.execute(
                "SELECT status FROM schema_migrations WHERE migration_name = 'broken_migration'"
            ).fetchone()[0]
            == "failed"
        )
