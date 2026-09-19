"""真实 Saver、checkpoint 和 canonical 存储的一致性合同。"""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.core.path_utils import get_session_path_resolver
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.system_reminder_checkpoint_service import (
    append_system_reminder_checkpoint,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)
from app.services.infrastructure.rollout_context.storage.append_writer import (
    RolloutAppendWriter,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutFileLock,
)
from app.services.infrastructure.rollout_context.storage.service import (
    RolloutStorage,
    _RolloutOperationLock,
)
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader

SESSION_ID = "ses_e6d2707870e54cab8c135193c0802532"
LEGACY_SESSION_ID = "ses_58a5607fd562454a932d851c95b73cc4"


def _storage(sessions_dir: Path) -> RolloutStorage:
    """测试低层 storage 时显式注入 checkpoint 层的消息 codec。"""
    return RolloutStorage(
        sessions_dir,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )


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


def test_rollout_file_lock_times_out_instead_of_waiting_forever(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    lock_path = tmp_path / ".rollout.write.lock"
    original_flock = fcntl.flock

    def reject_nonblocking_lock(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_NB:
            raise BlockingIOError("test lock is held")
        original_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", reject_nonblocking_lock)
    lock = _RolloutFileLock(
        lock_path,
        exclusive=True,
        timeout_seconds=0.01,
    )

    with pytest.raises(TimeoutError, match="rollout 文件锁获取超时"):
        lock.acquire()


def test_rollout_operation_lock_times_out_when_same_process_writer_is_busy(
    tmp_path: Path,
) -> None:
    lock = _RolloutOperationLock(
        tmp_path / ".rollout.write.lock",
        timeout_seconds=0.01,
    )
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold_lock() -> None:
        with lock:
            holder_ready.set()
            release_holder.wait(timeout=1)

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_lock)
        assert holder_ready.wait(timeout=1)
        with pytest.raises(TimeoutError, match="rollout 进程内写锁获取超时"):
            lock.__enter__()
        release_holder.set()
        holder.result(timeout=1)


def test_rollout_operation_lock_releases_thread_lock_when_file_unlock_fails(
    tmp_path: Path,
) -> None:
    lock = _RolloutOperationLock(tmp_path / ".rollout.write.lock")
    lock._thread_lock.acquire()
    lock._depth = 1

    def fail_release() -> None:
        raise OSError("模拟解锁失败")

    lock._file_lock = SimpleNamespace(release=fail_release)

    with pytest.raises(OSError, match="模拟解锁失败"):
        lock.__exit__(None, None, None)

    assert lock._thread_lock.acquire(timeout=0.01)
    lock._thread_lock.release()


def test_rollout_history_preserves_more_than_32_thinking_blocks() -> None:
    from app.services.mapping.itemized.history_turns.content import thinking_blocks

    message = AIMessage(
        id="assistant-many-thinking-blocks",
        content=[
            {
                "type": "reasoning",
                "id": f"reasoning-{index}",
                "reasoning": f"思考块 {index}",
            }
            for index in range(33)
        ],
    )

    blocks = thinking_blocks([message])

    assert len(blocks) == 33
    assert blocks[-1].text == "思考块 32"


def test_rollout_preserves_langchain_invalid_tool_calls_field(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
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
        build_checkpoint_config(SESSION_ID),
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
            get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
            / "rollout"
            / "rollout.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assistant_records = [
        record for record in records if record["wire_role"] == "assistant"
    ]
    assert assistant_records[0]["payload"] == "普通响应"
    assert assistant_records[1]["payload"]["invalid_tool_calls"][0]["id"] == (
        "call-invalid"
    )
    assert all(record["format_version"] == 2 for record in records)
    assert all(record["record_type"] == "item" for record in records)
    assert all("message" not in record and "role" not in record for record in records)

    restored = saver.get_tuple(build_checkpoint_config(SESSION_ID))
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
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)

    config = build_checkpoint_config(
        SESSION_ID, checkpoint_id="unpersisted-initial-checkpoint"
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

    restored = saver.get_tuple(build_checkpoint_config(SESSION_ID))
    assert restored is not None
    assert restored.parent_config is None
    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT parent_checkpoint_id FROM checkpoints WHERE checkpoint_id = 'cp-root'"
            ).fetchone()[0]
            is None
        )


def test_put_repairs_empty_message_channel_version(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "cp-empty-message-version"
    checkpoint["channel_values"] = {
        "messages": [HumanMessage(content="消息版本为空", id="user-empty-version")],
    }
    checkpoint["channel_versions"] = {"messages": None}
    checkpoint["updated_channels"] = ["messages"]

    saver.put(
        build_checkpoint_config(SESSION_ID),
        checkpoint,
        {"source": "empty-message-version"},
        {},
    )

    restored = saver.get_tuple(build_checkpoint_config(SESSION_ID))
    assert restored is not None
    assert restored.checkpoint["channel_versions"]["messages"].startswith("checkpoint:")


def test_checkpoint_namespace_queries_do_not_cross_match(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    namespace_a = build_checkpoint_config(SESSION_ID, checkpoint_ns="ns-a")
    namespace_b = build_checkpoint_config(SESSION_ID, checkpoint_ns="ns-b")

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
        build_checkpoint_config(SESSION_ID),
        _checkpoint("cp-child", [HumanMessage(content="子进程写入", id="child-u")]),
        {"source": "cross-process-lock"},
        {"messages": "child"},
    )


def test_read_snapshot_keeps_sqlite_and_jsonl_watermark_consistent_across_processes(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint("cp-parent", [HumanMessage(content="父进程写入", id="parent-u")]),
        {"source": "snapshot-test"},
        {"messages": "parent"},
    )
    storage = _storage(sessions_dir)
    snapshot = storage.open_read_snapshot(SESSION_ID)
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
        build_checkpoint_config(SESSION_ID)
    )
    assert restored is not None
    assert restored.checkpoint["id"] == "cp-child"


def test_read_snapshot_does_not_touch_existing_rollout_files(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    storage.initialize(SESSION_ID)
    rollout_root = (
        get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
        / "rollout"
    )
    rollout_path = rollout_root / "rollout.jsonl"
    index_path = rollout_root / "index.sqlite"
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in (rollout_path, index_path)
    }
    time.sleep(0.01)
    snapshot = storage.open_read_snapshot(SESSION_ID)
    try:
        assert snapshot.connection.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        snapshot.close()
    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in (rollout_path, index_path)
    }
    assert after == before


def test_checkpoint_read_does_not_reinitialize_existing_rollout(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    saver = RolloutCheckpointSaver(sessions_dir, storage=storage)
    config = saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint(
            "cp-existing",
            [HumanMessage(content="只读 checkpoint", id="user-existing")],
        ),
        {"source": "read-without-recovery"},
        {"messages": "existing"},
    )

    def fail_initialize(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("已有 rollout 的 checkpoint 读取不应重新初始化")

    monkeypatch.setattr(storage, "initialize", fail_initialize)
    restored = saver.get_tuple(config)

    assert restored is not None
    assert restored.checkpoint["id"] == "cp-existing"


def test_delete_legacy_rollout_does_not_initialize_removed_layout(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, LEGACY_SESSION_ID)
    rollout_root = (
        get_session_path_resolver(sessions_dir).resolve_session_node(LEGACY_SESSION_ID)
        / "rollout"
    )
    rollout_root.mkdir(parents=True)
    (rollout_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    (rollout_root / "segment-001.jsonl").write_text("{}\n", encoding="utf-8")
    (rollout_root / "index.sqlite").touch()

    storage = _storage(sessions_dir)

    assert storage.pinned_fork_children(LEGACY_SESSION_ID) == ()
    storage.release_fork_retentions(LEGACY_SESSION_ID)
    storage.delete_thread(LEGACY_SESSION_ID)

    assert not rollout_root.exists()


def test_validate_index_uses_read_only_snapshot_for_maintenance_check(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    storage.initialize(SESSION_ID)

    snapshot = storage.validate_index(SESSION_ID)
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
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    first = _turn("turn-1", "001")
    config = saver.put(
        build_checkpoint_config(SESSION_ID),
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
    reader = RolloutHistoryReader(RolloutContextReader(_storage(sessions_dir)))
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
        SESSION_ID,
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
    from app.services.infrastructure.rollout_history.snapshot import (
        IndexedHistorySnapshots,
    )

    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    storage.initialize(SESSION_ID)
    context_reader = RolloutContextReader(storage)
    snapshots = IndexedHistorySnapshots(context_reader)
    snapshot = storage.open_read_snapshot(SESSION_ID)
    monkeypatch.setattr(
        context_reader,
        "open_snapshot",
        lambda _session_id: snapshot,
    )

    def fail_after_open(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("模拟索引解析失败")

    monkeypatch.setattr(snapshots, "_read_indexed_history_snapshot", fail_after_open)

    with pytest.raises(RuntimeError, match="模拟索引解析失败"):
        snapshots.read(SESSION_ID)
    assert snapshot.closed is True


def test_checkpoint_envelope_and_channels_are_authoritative_sqlite(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    messages = _turn("turn-1", "001")
    checkpoint_config = saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint("cp-1", messages, counter=3, task_state=None),
        {"source": "unit", "step": 1},
        {"messages": "1", "counter": "1", "task_state": "1"},
    )
    saver.finalize_turn(
        session_id=SESSION_ID,
        turn_id="turn-1",
        final_message_id="final-001",
    )

    restored = saver.get_tuple(checkpoint_config)
    assert restored is not None
    assert [
        message.id for message in restored.checkpoint["channel_values"]["messages"]
    ] == [message.id for message in messages]
    assert restored.checkpoint["channel_values"]["counter"] == 3
    assert restored.checkpoint["channel_values"]["task_state"] is None

    rollout_root = (
        get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
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
    assert [line["semantic_kind"] for line in lines] == [
        "user_input", "assistant_output", "tool_call", "tool_result", "assistant_output"
    ]
    assert [line["item_sequence"] for line in lines] == [1, 2, 3, 4, 5]
    assert lines[1]["payload"] == "检查 001"
    assert lines[1]["message_group_id"] == lines[2]["message_group_id"]
    assert restored.checkpoint["channel_values"]["messages"][1].content == messages[1].content
    assert {line["wire_role"] for line in lines} == {"user", "assistant", "tool"}
    assert all(line["record_type"] == "item" for line in lines)
    assert all("message" not in line and "role" not in line for line in lines)
    with sqlite3.connect(rollout_root / "index.sqlite") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM control_events").fetchone()[0] >= 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM checkpoint_channels").fetchone()[0]
            == 3
        )
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone()[0] == 5
        assert len(connection.execute(
            "SELECT DISTINCT commit_id FROM item_catalog WHERE item_id IN (?, ?)",
            (lines[1]["item_id"], lines[2]["item_id"]),
        ).fetchall()) == 1


def test_failed_turn_status_survives_history_reload(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
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
        build_checkpoint_config(SESSION_ID),
        _checkpoint("cp-failed", messages),
        {"source": "failed-turn-test"},
        {"messages": "1"},
    )

    writer = RolloutAppendWriter(sessions_dir)
    assert (
        writer.mark_turn_terminal_status(
            session_id=SESSION_ID,
            turn_id="failed-turn",
            status="failed",
        )
        is True
    )

    page = RolloutHistoryReader(RolloutContextReader(_storage(sessions_dir))).load(
        SESSION_ID,
        TurnHistoryLoadRequest(direction="tail", turns=1),
    )
    assert page.items[0].turn_id == "failed-turn"
    assert page.items[0].status.value == "failed"


def test_terminal_turn_status_convergence_is_idempotent_after_termination(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """Turn 已收敛后，迟到的 job_failed 收敛不能抛错。

    否则启动恢复会读到同一条历史 ``job_failed`` Trace 再次崩溃并写下新的失败
    事件，形成每次启动都失败的死循环。
    """
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    messages = [
        HumanMessage(
            content="会被取消的 Turn",
            id="cancelled-user",
            response_metadata={
                "message_metadata": {
                    "turn_id": "cancelled-turn",
                    "job_id": "cancelled-turn",
                }
            },
        )
    ]
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint("cp-cancelled", messages),
        {"source": "cancelled-turn-test"},
        {"messages": "1"},
    )

    def turn_status() -> str:
        with sqlite3.connect(
            get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
            / "rollout"
            / "index.sqlite"
        ) as connection:
            return connection.execute(
                "SELECT status FROM turn_records WHERE turn_id = ?",
                ("cancelled-turn",),
            ).fetchone()[0]

    assert (
        saver.mark_turn_terminal_status(
            session_id=SESSION_ID,
            turn_id="cancelled-turn",
            status="cancelled",
        )
        is True
    )
    assert turn_status() == "cancelled"
    # 与终态不同的收敛请求必须幂等返回，而不是抛"非法 Turn.status 转移"。
    assert (
        saver.mark_turn_terminal_status(
            session_id=SESSION_ID,
            turn_id="cancelled-turn",
            status="failed",
        )
        is True
    )
    assert turn_status() == "cancelled"


def test_hidden_system_reminder_does_not_create_empty_chat_turn(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    user = HumanMessage(
        content="带工具的任务",
        id="user-job-1",
        response_metadata={"message_metadata": {"turn_id": "job-1", "job_id": "job-1"}},
    )
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint("cp-1", [user]),
        {"source": "test"},
        {"messages": "1"},
    )

    assert (
        append_system_reminder_checkpoint(
            checkpointer=saver,
            session_id=SESSION_ID,
            reminder="任务已超时，请根据已完成结果明确报告失败。",
            response_metadata={"source": "job_timeout"},
            checkpoint_source="job_timeout",
        )
        is True
    )

    with sqlite3.connect(
        get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
        / "rollout"
        / "index.sqlite"
    ) as connection:
        turns = connection.execute(
            "SELECT turn_id, status FROM turns ORDER BY turn_ordinal"
        ).fetchall()
        reminder = connection.execute(
            "SELECT turn_id, visibility FROM messages WHERE role = 'user' AND message_id != ?",
            ("user-job-1",),
        ).fetchone()
        reminder_item = connection.execute(
            "SELECT semantic_kind, turn_id, turn_scope, status FROM item_catalog "
            "WHERE semantic_kind = 'runtime_notice'"
        ).fetchone()

    assert turns == [("job-1", "running")]
    assert reminder is not None
    assert reminder[0].startswith("internal-")
    assert reminder[1] == "internal"
    assert reminder_item is not None
    assert reminder_item[0:3] == ("runtime_notice", None, "pending_next_turn")
    assert reminder_item[3] == "completed"

    assert (
        saver.mark_turn_terminal_status(
            session_id=SESSION_ID,
            turn_id="job-1",
            status="failed",
        )
        is True
    )
    page = RolloutHistoryReader(RolloutContextReader(_storage(sessions_dir))).load(
        SESSION_ID,
        TurnHistoryLoadRequest(direction="tail", turns=8),
    )
    assert [item.turn_id for item in page.items] == ["job-1"]
    assert page.items[0].status.value == "failed"


def test_legacy_hidden_reminder_turn_is_excluded_from_history(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    legacy_reminder = HumanMessage(
        content="<system_reminder>旧的超时提醒</system_reminder>",
        id="legacy-reminder",
        response_metadata={
            "internal": True,
            # 模拟修复前已把隐藏提醒错误写成 normal Turn 的索引形态。
            "turn_id": "turn-legacy-reminder",
        },
    )
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint("cp-legacy", [legacy_reminder]),
        {"source": "legacy-reminder"},
        {"messages": "1"},
    )

    page = RolloutHistoryReader(RolloutContextReader(_storage(sessions_dir))).load(
        SESSION_ID,
        TurnHistoryLoadRequest(direction="tail", turns=8),
    )

    assert page.items == []


def test_rewind_replay_uses_new_canonical_suffix_without_replacement_event(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    first = _turn("turn-1", "001")
    first_config = saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint("cp-1", first),
        {"source": "unit"},
        {"messages": "1"},
    )
    saver.finalize_turn(
        session_id=SESSION_ID, turn_id="turn-1", final_message_id="final-001"
    )
    second_config = saver.put(
        first_config,
        _checkpoint("cp-2", [*first, *_turn("turn-2", "002")]),
        {"source": "unit"},
        {"messages": "2"},
    )
    root = (
        get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
        / "rollout"
    )
    immutable_prefix = (root / "rollout.jsonl").read_bytes()
    saver.rewind(build_checkpoint_config(SESSION_ID), checkpoint_id="cp-1")
    assert (root / "rollout.jsonl").read_bytes() == immutable_prefix
    replay = [first[0], AIMessage(content="编辑后的响应", id="replay-a")]
    replay_config = saver.put(
        build_checkpoint_config(SESSION_ID, checkpoint_id="cp-1"),
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
    replay_bytes = (root / "rollout.jsonl").read_bytes()
    assert replay_bytes.startswith(immutable_prefix)
    before_items = [json.loads(line) for line in immutable_prefix.splitlines()]
    assert [item["semantic_kind"] for item in before_items] == [
        "user_input", "assistant_output", "tool_call", "tool_result", "assistant_output"
    ] * 2
    appended = [json.loads(line) for line in replay_bytes[len(immutable_prefix):].splitlines()]
    assert len(appended) == 1
    assert appended[0]["item_sequence"] == 11
    assert appended[0]["semantic_kind"] == "assistant_output"
    assert appended[0]["payload"] == "编辑后的响应"
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
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    config = saver.put(
        build_checkpoint_config(SESSION_ID),
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


def test_pending_write_corruption_is_rejected_instead_of_decoded(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    config = saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint("cp-1", [HumanMessage(content="状态", id="u1")]),
        {"source": "unit"},
        {"messages": "1"},
    )
    saver.put_writes(config, [("counter", 1)], "task-1", "node-a")
    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        connection.execute(
            "UPDATE pending_writes SET value_length = value_length + 1 WHERE checkpoint_id = ?",
            (config["configurable"]["checkpoint_id"],),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="pending_writes.value_length"):
        saver.get_tuple(config)


def test_uncommitted_jsonl_tail_is_truncated_but_sqlite_loss_is_explicit_failure(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint("cp-1", [HumanMessage(content="已提交", id="u1")]),
        {"source": "unit"},
        {"messages": "1"},
    )
    root = (
        get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
        / "rollout"
    )
    path = root / "rollout.jsonl"
    committed_size = path.stat().st_size
    with path.open("ab") as stream:
        stream.write(
            b'{"sequence":999,"message_id":"tail","turn_id":"t","role":"user","message":{}}\n'
        )
    assert path.stat().st_size > committed_size
    _storage(sessions_dir).initialize(SESSION_ID)
    assert path.stat().st_size == committed_size

    (root / "index.sqlite").write_bytes(b"not sqlite")
    with pytest.raises((sqlite3.DatabaseError, RuntimeError)):
        RolloutCheckpointSaver(sessions_dir).get_tuple(
            build_checkpoint_config(SESSION_ID)
        )


def test_half_line_and_fsync_failure_never_become_committed_messages(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    path = (
        get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
        / "rollout"
        / "rollout.jsonl"
    )

    def fail_fsync(_stream: object) -> None:
        raise OSError("fsync injected failure")

    monkeypatch.setattr(
        "app.services.infrastructure.rollout_context.storage.maintenance.os.fsync",
        fail_fsync,
    )
    with pytest.raises(OSError, match="fsync"):
        saver.put(
            build_checkpoint_config(SESSION_ID),
            _checkpoint("cp-fsync", [HumanMessage(content="未提交", id="u-fsync")]),
            {"source": "fsync-test"},
            {"messages": "1"},
        )
    assert path.stat().st_size == 0
    monkeypatch.setattr(
        "app.services.infrastructure.rollout_context.storage.maintenance.os.fsync",
        lambda _stream: None,
    )
    _storage(sessions_dir).initialize(SESSION_ID)
    assert path.read_bytes() == b""
    path.write_bytes(b'{"sequence":1,"message_id":"half"')
    _storage(sessions_dir).initialize(SESSION_ID)
    assert path.read_bytes() == b""


def test_sqlite_commit_window_is_retryable_without_duplicate_jsonl(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    config = build_checkpoint_config(SESSION_ID)
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
        get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
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
        checkpointer._storage is checkpointer._history_reader._context_reader._storage
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
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)

    def append(index: int) -> None:
        saver.put(
            build_checkpoint_config(SESSION_ID),
            _checkpoint(
                f"cp-concurrent-{index}",
                [HumanMessage(content=f"并发 {index}", id=f"u-concurrent-{index}")],
            ),
            {"source": "concurrency-test", "step": index},
            {"messages": str(index)},
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(append, range(4)))

    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
    records = [
        json.loads(line)
        for line in (root / "rollout" / "rollout.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == 4
    assert {record["item_id"] for record in records} == {
        f"item-u-concurrent-{index}" for index in range(4)
    }


def test_repeating_same_checkpoint_is_idempotent_but_conflicting_payload_fails(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    config = build_checkpoint_config(SESSION_ID)
    checkpoint = _checkpoint(
        "cp-idempotent",
        [HumanMessage(content="只写一次", id="u-idempotent")],
    )
    metadata = {"source": "idempotency"}
    versions = {"messages": "1"}
    first_config = saver.put(config, checkpoint, metadata, versions)
    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
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


def test_checkpoint_retry_rejects_corrupted_commit_pointer(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    config = build_checkpoint_config(SESSION_ID)
    checkpoint = _checkpoint(
        "cp-corrupted-pointer",
        [HumanMessage(content="commit pointer", id="u-pointer")],
    )
    metadata = {"source": "commit-pointer"}
    versions = {"messages": "1"}
    saver.put(config, checkpoint, metadata, versions)
    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        connection.execute(
            "UPDATE checkpoints SET commit_id = 999999 WHERE checkpoint_id = ?",
            (checkpoint["id"],),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="不存在的 storage commit"):
        RolloutCheckpointSaver(sessions_dir).put(
            config,
            checkpoint,
            metadata,
            versions,
        )


def test_checkpoint_view_without_new_items_reuses_previous_commit(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    """纯 view 更新不能伪造 terminal_convergence 或推进 JSONL offset。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    first_config = saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint(
            "cp-view-source",
            [HumanMessage(content="同一个 canonical item", id="view-user")],
        ),
        {"source": "view-source"},
        {"messages": "1"},
    )
    second_config = saver.put(
        first_config,
        _checkpoint(
            "cp-view-only",
            [HumanMessage(content="同一个 canonical item", id="view-user")],
        ),
        {"source": "view-only"},
        {"messages": "2"},
    )

    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        commit_count, first_commit, second_commit, last_commit = connection.execute(
            "SELECT COUNT(*), "
            "(SELECT commit_id FROM checkpoints WHERE checkpoint_id = 'cp-view-source'), "
            "(SELECT commit_id FROM checkpoints WHERE checkpoint_id = 'cp-view-only'), "
            "(SELECT last_commit_id FROM database_meta WHERE singleton_id = 1)"
        ).fetchone()
        kinds = connection.execute(
            "SELECT commit_kind, commit_mode, jsonl_record_count FROM storage_commits"
        ).fetchall()
    assert second_config["configurable"]["checkpoint_id"] == "cp-view-only"
    assert (commit_count, first_commit, second_commit, last_commit) == (
        1,
        first_commit,
        first_commit,
        first_commit,
    )
    assert kinds == [("item_convergence", "item_bearing", 1)]


def test_read_rejects_broken_storage_commit_offset_chain(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint(
            "cp-broken-commit-chain",
            [HumanMessage(content="提交链完整性", id="user-broken-chain")],
        ),
        {"source": "broken-commit-chain"},
        {"messages": "1"},
    )
    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        connection.execute(
            "UPDATE storage_commits SET jsonl_offset_after = jsonl_offset_after - 1 "
            "WHERE commit_id = (SELECT MAX(commit_id) FROM storage_commits)"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="JSONL offset 链断裂"):
        saver.get_tuple(build_checkpoint_config(SESSION_ID))


def test_read_rejects_storage_commit_with_incomplete_item_catalog(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint(
            "cp-broken-commit-catalog",
            [HumanMessage(content="提交 catalog 不完整", id="user-broken-catalog")],
        ),
        {"source": "broken-commit-catalog"},
        {"messages": "1"},
    )
    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        connection.execute(
            "UPDATE storage_commits SET jsonl_record_count = jsonl_record_count + 1 "
            "WHERE commit_id = (SELECT MAX(commit_id) FROM storage_commits)"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="item catalog 数量不一致"):
        saver.get_tuple(build_checkpoint_config(SESSION_ID))


def test_read_rejects_catalog_locator_that_does_not_match_canonical_jsonl(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint(
            "cp-broken-item-locator",
            [HumanMessage(content="损坏 item locator", id="user-broken-item")],
        ),
        {"source": "broken-item-locator"},
        {"messages": "1"},
    )
    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        connection.execute(
            "UPDATE item_catalog SET jsonl_length = jsonl_length - 1 "
            "WHERE item_id = 'item-user-broken-item'"
        )
        connection.commit()

    # 摘要 snapshot 先校验 SQLite locator 边界，无需打开正文才发现少一个字节。
    with pytest.raises(RuntimeError, match="committed_jsonl_offset.*不一致"):
        saver.get_tuple(build_checkpoint_config(SESSION_ID))


def test_rollout_schema_exposes_all_authoritative_tables_and_core_constraints(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    storage.initialize(SESSION_ID)
    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
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
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    config = saver.put(
        build_checkpoint_config(SESSION_ID),
        _checkpoint("cp-1", [HumanMessage(content="可恢复", id="u1")], counter=1),
        {"source": "backup-test"},
        {"messages": "1", "counter": "1"},
    )
    storage = _storage(sessions_dir)
    backup = storage.backup_index(SESSION_ID, destination=tmp_path / "index.bak")
    index_path = storage.index_path(SESSION_ID)
    index_path.write_bytes(b"corrupted")

    restored_snapshot = storage.restore_index_backup_offline(SESSION_ID, backup)
    try:
        assert restored_snapshot.manifest.latest_checkpoint_id == "cp-1"
    finally:
        restored_snapshot.close()
    quarantine_entries = tuple(
        (index_path.parent / "recovery-quarantine").iterdir()
    )
    assert len(quarantine_entries) == 1
    quarantine = quarantine_entries[0]
    assert (quarantine / "index.sqlite").read_bytes() == b"corrupted"
    restore_manifest = json.loads(
        (quarantine / "restore-manifest.json").read_text(encoding="utf-8")
    )
    assert restore_manifest["schema"] == "rollout-index-offline-restore:v1"
    assert restore_manifest["session_id"] == SESSION_ID
    assert restore_manifest["source_path"] == str(backup)
    assert restore_manifest["source_sha256"] == restore_manifest["installed_sha256"]
    assert restore_manifest["quarantined"] == [
        {
            "name": "index.sqlite",
            "sha256": storage._file_hash(quarantine / "index.sqlite"),
        }
    ]
    restored = RolloutCheckpointSaver(sessions_dir).get_tuple(config)
    assert restored is not None
    assert restored.checkpoint["channel_values"]["counter"] == 1


def test_sqlite_backup_failure_does_not_leave_partial_temporary_index(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    storage.initialize(SESSION_ID)
    index_path = storage.index_path(SESSION_ID)
    index_path.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(RuntimeError, match="recovery_required") as caught:
        storage.backup_index(SESSION_ID, destination=tmp_path / "index.bak")

    assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_sqlite_restore_failure_does_not_leave_partial_temporary_index(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    storage.initialize(SESSION_ID)
    broken_backup = tmp_path / "broken-index.bak"
    broken_backup.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(sqlite3.DatabaseError):
        storage.restore_index_backup(SESSION_ID, broken_backup)

    rollout_root = storage.index_path(SESSION_ID).parent
    assert not tuple(rollout_root.glob(".index.sqlite.*.restore"))


def test_schema_migration_checksum_and_completion_are_authoritative(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    storage.initialize(SESSION_ID)
    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        connection.execute(
            "UPDATE schema_migrations SET migration_checksum = 'broken' WHERE from_version = 0"
        )
    with pytest.raises(RuntimeError, match="checksum"):
        storage.initialize(SESSION_ID)


def test_schema_migration_runs_transactionally_and_keeps_backup(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    storage.initialize(SESSION_ID)
    target_version = storage_version.ROLLOUT_SCHEMA_VERSION + 1
    monkeypatch.setattr(
        storage_version,
        "ROLLOUT_SCHEMA_VERSION",
        target_version,
    )

    snapshot = storage.migrate_schema(
        SESSION_ID,
        to_version=target_version,
        migration_name="add_migration_probe_index",
        migration_sql="CREATE INDEX migration_probe ON messages(message_id)",
    )
    try:
        assert snapshot.manifest.rollout_id.startswith("rollout-")
    finally:
        snapshot.close()
    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT schema_version FROM database_meta WHERE singleton_id = 1"
            ).fetchone()[0]
            == target_version
        )
        assert (
            connection.execute(
                "SELECT status FROM schema_migrations WHERE to_version = ?",
                (target_version,),
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
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    storage.initialize(SESSION_ID)
    target_version = storage_version.ROLLOUT_SCHEMA_VERSION + 1
    monkeypatch.setattr(
        storage_version,
        "ROLLOUT_SCHEMA_VERSION",
        target_version,
    )

    with pytest.raises(sqlite3.OperationalError):
        storage.migrate_schema(
            SESSION_ID,
            to_version=target_version,
            migration_name="broken_migration",
            migration_sql="ALTER TABLE table_that_does_not_exist ADD COLUMN value TEXT",
        )

    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
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
