"""真实 Saver、checkpoint 和 canonical 存储的一致性合同。"""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.job_context import reset_current_job_id, set_current_job_id
from app.core.path_utils import catalog_provider_scope, get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.system_reminder_checkpoint_service import (
    submit_checkpoint_reminder,
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
from app.services.infrastructure.rollout_context.checkpoint.writer import (
    RolloutAppendWriter,
)
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutFileLock,
    _RolloutOperationLock,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from tests.support.session_catalog_owner import fixture_catalog_scope
from tests.support.workspaces import prepare_default_test_workspace

SESSION_ID = "ses_019c205391bb7a4b81b6728df70f63e6"
LEGACY_SESSION_ID = "ses_019b9187b87f78c78ef242bbd6043bfc"


def _accept_user_execution(
    saver: RolloutCheckpointSaver,
    session_id: str,
    thread_id: str,
    message: HumanMessage,
    *,
    turn_id: str,
    checkpoint_ns: str = "",
) -> ExecutionAdmission:
    """通过真实 ingress 接受原输入，并从 SQLite 取得该执行的准入身份。"""
    codec = saver._storage.storage._codec()
    message_id = codec.message_id(message, 0)
    execution_id = f"execution-{turn_id}"
    root = codec.items_for_message(
        message,
        item_sequence=1,
        message_id=message_id,
        turn_id=turn_id,
        execution_id=execution_id,
        timestamp="2026-10-05T00:00:00+00:00",
    )[0]
    accepted = saver.accept_turn(
        session_id,
        thread_id=thread_id,
        accepted_ingress_id=message_id,
        acceptance_idempotency_key=f"acceptance-{turn_id}",
        job_id=f"job-{turn_id}",
        admission_idempotency_key=f"admission-{turn_id}",
        payload=root.payload,
        payload_kind=root.payload_kind,
        turn_id=turn_id,
        root_item_id=root.item_id,
        initial_execution_id=execution_id,
        extensions=root.extensions,
        provenance_relations=root.provenance_relations,
        checkpoint_ns=checkpoint_ns,
    )
    assert accepted["initial_execution_id"] == execution_id
    return saver.execution_admission(
        session_id,
        thread_id=thread_id,
        execution_id=execution_id,
        checkpoint_ns=checkpoint_ns,
    )


@contextmanager
def _execution_scope(admission: ExecutionAdmission) -> Iterator[None]:
    """只绑定 owner 已核验的 admission，每个进程和线程显式进入自己的作用域。"""
    token = set_current_job_id(admission.job_id)
    try:
        with (
            session_thread_owner_scope(
                MutationIntentOwner(admission.session_id, admission.thread_id),
                langgraph_thread_id=admission.thread_id,
            ),
            execution_admission_scope(
                admission, langgraph_thread_id=admission.thread_id
            ),
        ):
            yield
    finally:
        reset_current_job_id(token)


def _storage(sessions_dir: Path) -> RolloutContextStore:
    """测试低层 storage 时显式注入 checkpoint 层的消息 codec。"""
    return RolloutContextStore(
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX 共享文件锁的非阻塞验证")
def test_read_items_borrows_writer_transaction_and_keeps_file_lock(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    for session_id in (SESSION_ID, LEGACY_SESSION_ID):
        session_bundle_factory(sessions_dir, session_id)
    with RolloutCheckpointSaver(sessions_dir) as saver:
        storage = saver._storage.storage
        thread_id = storage.main_thread_id(SESSION_ID)
        other_thread_id = storage.main_thread_id(LEGACY_SESSION_ID)
        _accept_user_execution(
            saver,
            SESSION_ID,
            thread_id,
            HumanMessage(content="writer 内的真实输入", id="writer-read-user"),
            turn_id="writer-read-turn",
        )
        original_items = storage.read_items(SESSION_ID, thread_id)
        assert len(original_items) == 1
        original_jsonl = storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes()
        lock_path = storage.root(SESSION_ID, thread_id=thread_id).parent / ".rollout.write.lock"
        with (
            storage._lock(thread_id, "", session_id=SESSION_ID),
            storage._connect(thread_id, "", session_id=SESSION_ID) as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            assert storage.read_items(
                SESSION_ID, thread_id, connection=connection,
            ) == original_items
            assert connection.in_transaction
            # 读取后同一写锁仍被持有，另一真实句柄不能取得共享锁。
            shared_lock = _RolloutFileLock(
                lock_path, exclusive=False, timeout_seconds=0.01,
            )
            with pytest.raises(TimeoutError, match="rollout 文件锁获取超时"):
                shared_lock.acquire()
            with pytest.raises(ValueError, match="writer connection 与目标 rollout owner 不一致"):
                storage.read_items(
                    LEGACY_SESSION_ID, other_thread_id, connection=connection,
                )
            connection.execute("SAVEPOINT corrupt_catalog")
            connection.execute("UPDATE item_catalog SET content_hash=?", ("a" * 64,))
            with pytest.raises(RuntimeError, match="catalog 与 JSONL content_hash 不一致"):
                storage.read_items(SESSION_ID, thread_id, connection=connection)
            assert connection.in_transaction
            connection.execute("ROLLBACK TO corrupt_catalog")
            connection.execute("RELEASE corrupt_catalog")
        assert storage.read_items(SESSION_ID, thread_id) == original_items
        assert storage.jsonl_path(SESSION_ID, thread_id=thread_id).read_bytes() == original_jsonl
        with (
            storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot,
            pytest.raises(ValueError, match="不能同时指定 snapshot 和 connection"),
        ):
            storage.read_items(
                SESSION_ID, thread_id, snapshot=snapshot,
                connection=snapshot.connection,
            )


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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="检查文件", id="user-1")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-invalid-tool-call"
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "cp-invalid-tool-call",
                [user, valid, invalid],
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
        assert all(
            "message" not in record and "role" not in record for record in records
        )

        restored = saver.get_tuple(build_checkpoint_config(thread_id))
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id

    config = build_checkpoint_config(
        thread_id, checkpoint_id="unpersisted-initial-checkpoint"
    )
    user = HumanMessage(content="首条消息", id="user-root")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-root"
    )
    with _execution_scope(admission):
        saver.put(
            config,
            _checkpoint(
                "cp-root",
                [user],
            ),
            {"source": "root-parent-test"},
            {"messages": "1"},
        )

        restored = saver.get_tuple(build_checkpoint_config(thread_id))
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "cp-empty-message-version"
    checkpoint["channel_values"] = {
        "messages": [HumanMessage(content="消息版本为空", id="user-empty-version")],
    }
    checkpoint["channel_versions"] = {"messages": None}
    checkpoint["updated_channels"] = ["messages"]

    admission = _accept_user_execution(
        saver,
        SESSION_ID,
        thread_id,
        checkpoint["channel_values"]["messages"][0],
        turn_id="turn-empty-message-version",
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
            checkpoint,
            {"source": "empty-message-version"},
            {},
        )

        restored = saver.get_tuple(build_checkpoint_config(thread_id))
        assert restored is not None
        assert restored.checkpoint["channel_versions"]["messages"].startswith(
            "checkpoint:"
        )


def test_checkpoint_namespace_queries_do_not_cross_match(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    namespace_a = build_checkpoint_config(thread_id, checkpoint_ns="ns-a")
    namespace_b = build_checkpoint_config(thread_id, checkpoint_ns="ns-b")

    user_a = HumanMessage(content="A", id="user-a")
    admission_a = _accept_user_execution(
        saver, SESSION_ID, thread_id, user_a, turn_id="turn-a", checkpoint_ns="ns-a"
    )
    with _execution_scope(admission_a):
        saver.put(
            namespace_a,
            _checkpoint("cp-ns-a", [user_a]),
            {"source": "namespace-a"},
            {"messages": "a"},
        )
    user_b = HumanMessage(content="B", id="user-b")
    admission_b = _accept_user_execution(
        saver, SESSION_ID, thread_id, user_b, turn_id="turn-b", checkpoint_ns="ns-b"
    )
    with _execution_scope(admission_b):
        saver.put(
            namespace_b,
            _checkpoint("cp-ns-b", [user_b]),
            {"source": "namespace-b"},
            {"messages": "b"},
        )

        restored_a = saver.get_tuple(namespace_a)
        restored_b = saver.get_tuple(namespace_b)
        assert restored_a is not None
        assert restored_b is not None
        assert restored_a.checkpoint["id"] == "cp-ns-a"
        assert restored_b.checkpoint["id"] == "cp-ns-b"
        assert [item.checkpoint["id"] for item in saver.list(namespace_a)] == [
            "cp-ns-a"
        ]
        assert [item.checkpoint["id"] for item in saver.list(namespace_b)] == [
            "cp-ns-b"
        ]


def _append_checkpoint_in_child(sessions_dir: str, thread_id: str) -> None:
    with fixture_catalog_scope(), RolloutCheckpointSaver(sessions_dir) as saver:
        assert saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id == thread_id
        user = HumanMessage(content="子进程写入", id="child-u")
        admission = _accept_user_execution(
            saver, SESSION_ID, thread_id, user, turn_id="turn-child"
        )
        with _execution_scope(admission):
            saver.put(
                build_checkpoint_config(thread_id),
                _checkpoint("cp-child", [user]),
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="父进程写入", id="parent-u")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-parent"
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-parent", [user]),
            {"source": "snapshot-test"},
            {"messages": "parent"},
        )
    storage = _storage(sessions_dir)
    snapshot = storage.storage.open_read_snapshot(SESSION_ID, thread_id)
    child = multiprocessing.get_context("spawn").Process(
        target=_append_checkpoint_in_child,
        args=(str(sessions_dir), thread_id),
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
    with session_thread_owner_scope(
        MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id
    ):
        restored = RolloutCheckpointSaver(sessions_dir).get_tuple(
            build_checkpoint_config(thread_id)
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
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    storage.storage.initialize(SESSION_ID, thread_id=thread_id)
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
    snapshot = storage.storage.open_read_snapshot(SESSION_ID, thread_id)
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="只读 checkpoint", id="user-existing")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-existing"
    )
    with _execution_scope(admission):
        config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "cp-existing",
                [user],
            ),
            {"source": "read-without-recovery"},
            {"messages": "existing"},
        )

        def fail_initialize(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("已有 rollout 的 checkpoint 读取不应重新初始化")

        monkeypatch.setattr(storage.storage, "initialize", fail_initialize)
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
    thread_id = storage.storage.main_thread_id(LEGACY_SESSION_ID)

    assert storage.forks.pinned_fork_children(LEGACY_SESSION_ID) == ()
    storage.forks.release_fork_retentions(LEGACY_SESSION_ID)
    storage.forks.delete_thread(LEGACY_SESSION_ID, thread_id=thread_id)

    assert not rollout_root.exists()


def test_validate_index_uses_read_only_snapshot_for_maintenance_check(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    storage.storage.initialize(SESSION_ID, thread_id=thread_id)

    snapshot = storage.storage.validate_index(SESSION_ID, thread_id)
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    first = _turn("turn-1", "001")
    first_admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, first[0], turn_id="turn-1"
    )
    with _execution_scope(first_admission):
        config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", first),
            {"source": "keyset-test"},
            {"messages": "1"},
        )
    second = _turn("turn-2", "002")
    second_admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, second[0], turn_id="turn-2"
    )
    with _execution_scope(second_admission):
        saver.put(
            config,
            _checkpoint("cp-2", [*first, *second]),
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
        thread_id,
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
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    storage.storage.initialize(SESSION_ID, thread_id=thread_id)
    context_reader = RolloutContextReader(storage)
    snapshots = IndexedHistorySnapshots(context_reader)
    snapshot = storage.storage.open_read_snapshot(SESSION_ID, thread_id)
    monkeypatch.setattr(
        context_reader,
        "open_snapshot",
        lambda _session_id, _thread_id: snapshot,
    )

    def fail_after_open(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("模拟索引解析失败")

    monkeypatch.setattr(snapshots, "_read_indexed_history_snapshot", fail_after_open)

    with pytest.raises(RuntimeError, match="模拟索引解析失败"):
        snapshots.read(SESSION_ID, thread_id)
    assert snapshot.closed is True


def test_checkpoint_envelope_and_channels_are_authoritative_sqlite(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    messages = _turn("turn-1", "001")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, messages[0], turn_id="turn-1"
    )
    with _execution_scope(admission):
        checkpoint_config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", messages, counter=3, task_state=None),
            {"source": "unit", "step": 1},
            {"messages": "1", "counter": "1", "task_state": "1"},
        )
        saver.finalize_turn(
            session_id=SESSION_ID,
            thread_id=thread_id,
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
            "user_input",
            "assistant_output",
            "tool_call",
            "tool_result",
            "assistant_output",
        ]
        assert [line["item_sequence"] for line in lines] == [1, 2, 3, 4, 5]
        assert lines[1]["payload"] == "检查 001"
        assert lines[1]["message_group_id"] == lines[2]["message_group_id"]
        assert (
            restored.checkpoint["channel_values"]["messages"][1].content
            == messages[1].content
        )
        assert {line["wire_role"] for line in lines} == {"user", "assistant", "tool"}
        assert all(line["record_type"] == "item" for line in lines)
        assert all("message" not in line and "role" not in line for line in lines)
        with sqlite3.connect(rollout_root / "index.sqlite") as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM control_events").fetchone()[0]
                >= 2
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM checkpoint_channels"
                ).fetchone()[0]
                == 3
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone()[0]
                == 5
            )
            assert (
                len(
                    connection.execute(
                        "SELECT DISTINCT commit_id FROM item_catalog WHERE item_id IN (?, ?)",
                        (lines[1]["item_id"], lines[2]["item_id"]),
                    ).fetchall()
                )
                == 1
            )


def test_failed_turn_status_survives_history_reload(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    messages = [
        HumanMessage(
            content="失败后仍然可以重试",
            id="failed-user",
            response_metadata={
                "message_metadata": {"turn_id": "failed-turn", "job_id": "failed-turn"}
            },
        )
    ]
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, messages[0], turn_id="failed-turn"
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-failed", messages),
            {"source": "failed-turn-test"},
            {"messages": "1"},
        )

        writer = RolloutAppendWriter(sessions_dir)
        assert (
            writer.mark_turn_terminal_status(
                session_id=SESSION_ID,
                thread_id=thread_id,
                turn_id="failed-turn",
                status="failed",
            )
            is True
        )

        page = RolloutHistoryReader(RolloutContextReader(_storage(sessions_dir))).load(
            SESSION_ID,
            thread_id,
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
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
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, messages[0], turn_id="cancelled-turn"
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
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
                thread_id=thread_id,
                turn_id="cancelled-turn",
                status="cancelled",
            )
            is True
        )
        assert turn_status() == "cancelled"
        jsonl = saver._storage.storage.jsonl_path(SESSION_ID, thread_id=thread_id)
        committed_prefix = jsonl.read_bytes()
        index = saver._storage.storage.index_path(SESSION_ID, thread_id=thread_id)
        with sqlite3.connect(index) as connection:
            committed_count = connection.execute(
                "SELECT COUNT(*) FROM storage_commits"
            ).fetchone()[0]
        # 与终态不同的收敛请求必须幂等返回，而不是抛"非法 Turn.status 转移"。
        assert (
            saver.mark_turn_terminal_status(
                session_id=SESSION_ID,
                thread_id=thread_id,
                turn_id="cancelled-turn",
                status="failed",
            )
            is True
        )
        assert turn_status() == "cancelled"
        assert jsonl.read_bytes() == committed_prefix
        with sqlite3.connect(index) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM storage_commits").fetchone()[0]
                == committed_count
            )
        for dispatch in (saver.execution_for_turn, saver.dispatch_replay):
            with pytest.raises(ValueError, match="turn_not_resumable"):
                dispatch(SESSION_ID, thread_id=thread_id, turn_id="cancelled-turn")
        assert turn_status() == "cancelled"


def test_hidden_system_reminder_does_not_create_empty_chat_turn(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(
        content="带工具的任务",
        id="user-job-1",
        response_metadata={"message_metadata": {"turn_id": "job-1", "job_id": "job-1"}},
    )
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="job-1"
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", [user]),
            {"source": "test"},
            {"messages": "1"},
        )

        assert (
            submit_checkpoint_reminder(
                checkpointer=saver,
                session_id=SESSION_ID,
                thread_id=thread_id,
                reminder="任务已超时，请根据已完成结果明确报告失败。",
                response_metadata={"source": "job_timeout"},
                checkpoint_source="job_timeout",
                event_identity="timeout-event-job-1",
                execution_id=admission.execution_id,
            )
            is True
        )

        with sqlite3.connect(
            get_session_path_resolver(sessions_dir).resolve_thread_node(
                SESSION_ID, thread_id
            )
            / "rollout"
            / "index.sqlite"
        ) as connection:
            turns = connection.execute(
                "SELECT turn_id, status FROM turns ORDER BY turn_ordinal"
            ).fetchall()
            reminder = connection.execute(
                "SELECT turn_id, turn_scope, wire_role, content FROM item_projections "
                "WHERE semantic_kind = 'runtime_notice'"
            ).fetchone()
            reminder_item = connection.execute(
                "SELECT semantic_kind, turn_id, turn_scope, status FROM item_catalog "
                "WHERE semantic_kind = 'runtime_notice'"
            ).fetchone()

        assert turns == [("job-1", "running")]
        assert reminder is not None
        assert reminder[0] is None
        assert reminder[1] == "pending_next_turn"
        assert reminder[2] == "user"
        assert "任务已超时" in reminder[3]
        assert reminder_item is not None
        assert reminder_item[0:3] == ("runtime_notice", None, "pending_next_turn")
        assert reminder_item[3] == "completed"

        assert (
            saver.mark_turn_terminal_status(
                session_id=SESSION_ID,
                thread_id=thread_id,
                turn_id="job-1",
                status="failed",
            )
            is True
        )
        page = RolloutHistoryReader(RolloutContextReader(_storage(sessions_dir))).load(
            SESSION_ID,
            thread_id,
            TurnHistoryLoadRequest(direction="tail", turns=8),
        )
        assert [item.turn_id for item in page.items] == ["job-1"]
        assert page.items[0].status.value == "failed"


def test_checkpoint_reminder_reuses_event_identity_and_separates_events(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    admission = _accept_user_execution(
        saver,
        SESSION_ID,
        thread_id,
        HumanMessage(content="需要超时提醒的任务", id="user-reminder-events"),
        turn_id="turn-reminder-events",
    )

    assert submit_checkpoint_reminder(
        checkpointer=saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        reminder="请继续处理超时任务。",
        response_metadata={"source": "job_timeout", "attempt": "first"},
        checkpoint_source="job_timeout",
        event_identity="timeout-event-1", execution_id=admission.execution_id,
    )
    # metadata key 顺序变化不能破坏同一事件的 canonical identity。
    assert submit_checkpoint_reminder(
        checkpointer=saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        reminder="请继续处理超时任务。",
        response_metadata={"attempt": "first", "source": "job_timeout"},
        checkpoint_source="job_timeout",
        event_identity="timeout-event-1", execution_id=admission.execution_id,
    )
    assert submit_checkpoint_reminder(
        checkpointer=saver,
        session_id=SESSION_ID,
        thread_id=thread_id,
        reminder="请继续处理超时任务。",
        response_metadata={"source": "job_timeout", "attempt": "other"},
        checkpoint_source="job_timeout",
        event_identity="timeout-event-2", execution_id=admission.execution_id,
    )

    with sqlite3.connect(
        get_session_path_resolver(sessions_dir).resolve_thread_node(
            SESSION_ID, thread_id
        )
        / "rollout"
        / "index.sqlite"
    ) as connection:
        rows = connection.execute(
            "SELECT item_id, content FROM item_projections "
            "WHERE semantic_kind = 'runtime_notice' ORDER BY item_sequence"
        ).fetchall()

    assert len(rows) == 2
    assert rows[0][0] != rows[1][0]
    assert rows[0][1] == rows[1][1]


def test_checkpoint_reminder_source_transaction_rolls_back_jsonl_on_sqlite_failure(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="用户输入")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-before-reminder-failure"
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-before-reminder-failure", [user]),
            {"source": "test"},
            {"messages": "1"},
        )
        rollout_root = saver._storage.storage.root(SESSION_ID, "", thread_id=thread_id)
        jsonl_path = saver._storage.storage.jsonl_path(SESSION_ID, "", thread_id=thread_id)
        before_jsonl_size = jsonl_path.stat().st_size
        with saver._storage.storage._connect(
            saver._storage.storage.main_thread_id(SESSION_ID),
            "",
            read_only=True,
            session_id=SESSION_ID,
        ) as connection:
            before_meta = connection.execute(
                "SELECT last_item_sequence, committed_jsonl_offset "
                "FROM database_meta WHERE singleton_id = 1"
            ).fetchone()

        def fail_view_membership(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected SQLite view failure")

        monkeypatch.setattr(
            saver._storage.storage,
            "_append_context_view_items",
            fail_view_membership,
        )
        with pytest.raises(RuntimeError, match="injected SQLite view failure"):
            submit_checkpoint_reminder(
                checkpointer=saver,
                session_id=SESSION_ID,
                thread_id=thread_id,
                reminder="故障注入提醒",
                response_metadata={"source": "injected"},
                checkpoint_source="injected",
                event_identity="projection-failure", execution_id=admission.execution_id,
            )

        assert rollout_root.is_dir()
        assert jsonl_path.stat().st_size == before_jsonl_size
        with sqlite3.connect(rollout_root / "index.sqlite") as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM item_catalog WHERE semantic_kind = 'runtime_notice'"
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM item_projections WHERE semantic_kind = 'runtime_notice'"
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT last_item_sequence, committed_jsonl_offset "
                    "FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                == before_meta
            )


def test_legacy_hidden_reminder_turn_is_excluded_from_history(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    legacy_reminder = HumanMessage(
        content="<system_reminder>旧的超时提醒</system_reminder>",
        id="legacy-reminder",
        response_metadata={
            "internal": True,
            "message_id": "legacy-reminder",
            "created_at": "2026-10-05T00:00:00+00:00",
            # 模拟修复前已把隐藏提醒错误写成 normal Turn 的索引形态。
            "turn_id": "turn-legacy-reminder",
        },
    )
    admission = saver.admit_internal_execution(
        SESSION_ID,
        thread_id=thread_id,
        job_id="job-legacy-reminder",
        intent=InternalExecutionIntent(
            message_id="legacy-reminder",
            execution_id="execution-legacy-reminder",
            admission_idempotency_key="admission-legacy-reminder",
        ),
        message=legacy_reminder,
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-legacy", [legacy_reminder]),
            {"source": "legacy-reminder"},
            {"messages": "1"},
        )

        page = RolloutHistoryReader(RolloutContextReader(_storage(sessions_dir))).load(
            SESSION_ID,
            thread_id,
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    first = _turn("turn-1", "001")
    first_admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, first[0], turn_id="turn-1"
    )
    with _execution_scope(first_admission):
        first_config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", first),
            {"source": "unit"},
            {"messages": "1"},
        )
        saver.finalize_turn(
            session_id=SESSION_ID,
            thread_id=thread_id,
            turn_id="turn-1",
            final_message_id="final-001",
        )
        first_snapshot = saver.get_tuple(first_config)
        assert first_snapshot is not None
        first_saved_messages = first_snapshot.checkpoint["channel_values"]["messages"]
    second = _turn("turn-2", "002")
    second_admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, second[0], turn_id="turn-2"
    )
    with _execution_scope(second_admission):
        second_config = saver.put(
            first_config,
            _checkpoint("cp-2", [*first, *second]),
            {"source": "unit"},
            {"messages": "2"},
        )
    root = saver._storage.storage.root(SESSION_ID, thread_id=thread_id)
    immutable_prefix = (root / "rollout.jsonl").read_bytes()
    with session_thread_owner_scope(
        MutationIntentOwner(SESSION_ID, thread_id), langgraph_thread_id=thread_id
    ):
        saver.rewind(build_checkpoint_config(thread_id), checkpoint_id="cp-1")
    assert (root / "rollout.jsonl").read_bytes() == immutable_prefix
    replayed = saver.replay_as_new_turn(
        SESSION_ID,
        thread_id=thread_id,
        source_turn_id="turn-1",
        acceptance_idempotency_key="acceptance-checkpoint-replay",
    )
    replay_turn_id = str(replayed["turn_id"])
    assert replay_turn_id != "turn-1"
    assert replayed["replay_of_turn_id"] == "turn-1"
    assert replayed["root_input_item_id"] != "item-user-001"
    assert replayed["initial_execution_id"] != first_admission.execution_id
    replay_root = saver._storage.storage.read_items(
        SESSION_ID,
        thread_id,
        item_ids=(str(replayed["root_input_item_id"]),),
    )
    codec = saver._storage.storage._codec()
    replay_user = codec.from_dict(codec.project_message(replay_root))
    replay_admission = saver.execution_admission(
        SESSION_ID,
        thread_id=thread_id,
        execution_id=str(replayed["initial_execution_id"]),
    )
    with _execution_scope(replay_admission):
        replay_config = saver.put(
            build_checkpoint_config(thread_id, checkpoint_id="cp-1"),
            _checkpoint(
                "cp-3", [replay_user, AIMessage(content="编辑后的响应", id="replay-a")]
            ),
            {"source": "replay"},
            {"messages": "3"},
        )
        restored = saver.get_tuple(replay_config)
        second_snapshot = saver.get_tuple(second_config)

    assert restored is not None
    restored_messages = restored.checkpoint["channel_values"]["messages"]
    assert len(restored_messages) == len(first_saved_messages) + 2
    assert [
        codec.to_dict(message)
        for message in restored_messages[: len(first_saved_messages)]
    ] == [codec.to_dict(message) for message in first_saved_messages]
    assert [message.id for message in first_saved_messages] == [
        "user-001",
        "call-message-001",
        "result-001",
        "final-001",
    ]
    assert (
        restored_messages[1].tool_calls[0]["id"]
        == restored_messages[2].tool_call_id
        == "call-001"
    )
    expected_replay_root = codec.to_dict(replay_user)
    restored_replay_root = codec.to_dict(restored_messages[-2])
    expected_data = dict(expected_replay_root["data"])
    restored_data = dict(restored_replay_root["data"])
    expected_canonical_metadata = dict(expected_data.pop("response_metadata"))
    restored_projection_metadata = dict(restored_data.pop("response_metadata"))
    # checkpoint hydration 只补 SQLite 权威身份与时间，正文 projection 逐字段一致。
    assert restored_replay_root["type"] == expected_replay_root["type"]
    assert restored_data == expected_data
    hydrated_identity = {
        "message_id": restored_projection_metadata.pop("message_id"),
        "created_at": restored_projection_metadata.pop("created_at"),
        "updated_at": restored_projection_metadata.pop("updated_at"),
    }
    hydrated_message_metadata = dict(
        restored_projection_metadata.pop("message_metadata", {})
    )
    hydrated_identity["message_metadata"] = {
        "turn_id": hydrated_message_metadata.pop("turn_id", None)
    }
    assert hydrated_message_metadata == {}
    # canonical typed manifests/extensions 独立逐字联校，hydration 只补身份键。
    assert restored_projection_metadata == expected_canonical_metadata
    with sqlite3.connect(root / "index.sqlite") as connection:
        projected = connection.execute(
            "SELECT message_id, turn_id, created_at FROM messages WHERE message_id = ?",
            (replay_user.id,),
        ).fetchone()
    assert projected is not None
    assert hydrated_identity == {
        "message_id": projected[0],
        "created_at": projected[2],
        "updated_at": projected[2],
        "message_metadata": {"turn_id": projected[1]},
    }
    assert restored_messages[-1].id == "replay-a"
    assert restored_messages[-1].content == "编辑后的响应"
    assert second_snapshot is not None
    assert [
        message.id
        for message in second_snapshot.checkpoint["channel_values"]["messages"]
    ] == [message.id for message in [*first, *second]]
    assert not {message.id for message in second} & {
        message.id for message in restored_messages
    }
    assert second_config["configurable"]["checkpoint_id"] == "cp-2"
    replay_bytes = (root / "rollout.jsonl").read_bytes()
    assert replay_bytes.startswith(immutable_prefix)
    before_items = [json.loads(line) for line in immutable_prefix.splitlines()]
    assert [item["semantic_kind"] for item in before_items] == [
        "user_input",
        "assistant_output",
        "tool_call",
        "tool_result",
        "assistant_output",
    ] * 2
    appended = [
        json.loads(line) for line in replay_bytes[len(immutable_prefix) :].splitlines()
    ]
    assert [item["item_sequence"] for item in appended] == [11, 12]
    assert [item["semantic_kind"] for item in appended] == [
        "user_input",
        "assistant_output",
    ]
    assert appended[0]["item_id"] == replayed["root_input_item_id"]
    assert appended[1]["payload"] == "编辑后的响应"
    assert all(item["turn_id"] == replay_turn_id for item in appended)
    with sqlite3.connect(root / "index.sqlite") as connection:
        source_turn = connection.execute(
            "SELECT status, root_input_item_id, initial_execution_id FROM turn_records WHERE turn_id='turn-1'"
        ).fetchone()
        assert source_turn == (
            "completed",
            "item-user-001",
            first_admission.execution_id,
        )
        target_turn = connection.execute(
            "SELECT replay_of_turn_id, root_input_item_id, initial_execution_id FROM turn_records WHERE turn_id=?",
            (replay_turn_id,),
        ).fetchone()
        assert target_turn == (
            "turn-1",
            replayed["root_input_item_id"],
            replayed["initial_execution_id"],
        )
        source_acceptance = connection.execute(
            "SELECT accepted_ingress_id, acceptance_idempotency_key FROM turn_acceptances WHERE turn_id='turn-1'"
        ).fetchone()
        replay_acceptance = connection.execute(
            "SELECT accepted_ingress_id, acceptance_idempotency_key FROM turn_acceptances WHERE turn_id=?",
            (replay_turn_id,),
        ).fetchone()
        assert set(source_acceptance).isdisjoint(replay_acceptance)
        kinds = {
            row[0]
            for row in connection.execute("SELECT control_kind FROM control_events")
        }
        assert "rewind" in kinds
        assert "replacement" not in kinds
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='records'"
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="状态", id="u1")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-state"
    )
    with _execution_scope(admission):
        config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", [user], counter=0, optional=None),
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="状态", id="u1")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-state"
    )
    with _execution_scope(admission):
        config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", [user]),
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="已提交", id="u1")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-committed"
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", [user]),
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
        _storage(sessions_dir).storage.initialize(SESSION_ID, thread_id=thread_id)
        assert path.stat().st_size == committed_size

        (root / "index.sqlite").write_bytes(b"not sqlite")
        with pytest.raises((sqlite3.DatabaseError, RuntimeError)):
            RolloutCheckpointSaver(sessions_dir).get_tuple(
                build_checkpoint_config(thread_id)
            )


def test_half_line_and_fsync_failure_never_become_committed_messages(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    path = (
        get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
        / "rollout"
        / "rollout.jsonl"
    )

    def fail_fsync(_stream: object) -> None:
        raise OSError("fsync injected failure")

    monkeypatch.setattr(
        "app.services.infrastructure.rollout_context.storage.rollout_maintenance_owner.os.fsync",
        fail_fsync,
    )
    with pytest.raises(OSError, match="fsync"):
        _accept_user_execution(
            saver,
            SESSION_ID,
            thread_id,
            HumanMessage(content="未提交", id="u-fsync"),
            turn_id="turn-fsync",
        )
    assert path.stat().st_size == 0
    monkeypatch.setattr(
        "app.services.infrastructure.rollout_context.storage.rollout_maintenance_owner.os.fsync",
        lambda _stream: None,
    )
    _storage(sessions_dir).storage.initialize(SESSION_ID, thread_id=thread_id)
    assert path.read_bytes() == b""
    with sqlite3.connect(path.parent / "index.sqlite") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone()[0] == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM storage_commits").fetchone()[0]
            == 0
        )
    path.write_bytes(b'{"sequence":1,"message_id":"half"')
    _storage(sessions_dir).storage.initialize(SESSION_ID, thread_id=thread_id)
    assert path.read_bytes() == b""
    with sqlite3.connect(path.parent / "index.sqlite") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone()[0] == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM storage_commits").fetchone()[0]
            == 0
        )


def test_sqlite_commit_window_is_retryable_without_duplicate_jsonl(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    config = build_checkpoint_config(thread_id)
    checkpoint = _checkpoint(
        "cp-commit-window",
        [HumanMessage(content="已提交但调用方崩溃", id="u-commit-window")],
    )
    storage = saver._storage
    original_commit = storage.storage._commit_connection

    def commit_then_crash(connection: sqlite3.Connection) -> None:
        original_commit(connection)
        raise OSError("调用方在 SQLite commit 后崩溃")

    admission = _accept_user_execution(
        saver,
        SESSION_ID,
        thread_id,
        checkpoint["channel_values"]["messages"][0],
        turn_id="turn-commit-window",
    )
    with _execution_scope(admission):
        monkeypatch.setattr(storage.storage, "_commit_connection", commit_then_crash)
        with pytest.raises(OSError, match="SQLite commit"):
            saver.put(
                config, checkpoint, {"source": "commit-window"}, {"messages": "1"}
            )

        rollout_path = (
            get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
            / "rollout"
            / "rollout.jsonl"
        )
        committed_bytes = rollout_path.read_bytes()
        assert len(committed_bytes.splitlines()) == 1

        monkeypatch.setattr(storage.storage, "_commit_connection", original_commit)
        retry_config = saver.put(
            config,
            checkpoint,
            {"source": "commit-window"},
            {"messages": "1"},
        )
        assert retry_config["configurable"]["checkpoint_id"] == "cp-commit-window"
        assert rollout_path.read_bytes() == committed_bytes
        assert saver.get_tuple(retry_config) is not None


@pytest.fixture
def container_workspaces(integration_workspace_root_path: str) -> tuple[Path, Path]:
    """完整模板工作区由正式镜像 fixture 提供，第二个工作区也位于该文件的 workspace 树。"""
    first = Path(integration_workspace_root_path)
    second = prepare_default_test_workspace(
        workspace_root=first / "other-workspace",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    return first, second


def test_application_container_exposes_one_checkpoint_entrypoint(
    container_workspaces: tuple[Path, Path],
) -> None:
    from app.container import build_app_container

    container = build_app_container(workspace_root=container_workspaces[0])
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
    other_container = build_app_container(workspace_root=container_workspaces[1])
    assert (
        checkpointer._storage
        is not other_container.message_service._checkpointer._storage
    )
    assert (
        container.session_subagent_service._parent_session_reader
        is container.session_service
    )
    assert (
        container.terminal_steering_service._session_orchestrator
        is container.session_orchestrator
    )


def test_concurrent_checkpoint_appends_do_not_interleave_jsonl(
    tmp_path: Path,
    session_bundle_factory,
    session_catalog_owner,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id

    def append(index: int) -> None:
        with catalog_provider_scope(session_catalog_owner):
            user = HumanMessage(content=f"并发 {index}", id=f"u-concurrent-{index}")
            admission = _accept_user_execution(
                saver, SESSION_ID, thread_id, user, turn_id=f"turn-concurrent-{index}"
            )
            with _execution_scope(admission):
                saver.put(
                    build_checkpoint_config(thread_id),
                    _checkpoint(f"cp-concurrent-{index}", [user]),
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    config = build_checkpoint_config(thread_id)
    checkpoint = _checkpoint(
        "cp-idempotent",
        [HumanMessage(content="只写一次", id="u-idempotent")],
    )
    metadata = {"source": "idempotency"}
    versions = {"messages": "1"}
    admission = _accept_user_execution(
        saver,
        SESSION_ID,
        thread_id,
        checkpoint["channel_values"]["messages"][0],
        turn_id="turn-idempotent",
    )
    with _execution_scope(admission):
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    config = build_checkpoint_config(thread_id)
    checkpoint = _checkpoint(
        "cp-corrupted-pointer",
        [HumanMessage(content="commit pointer", id="u-pointer")],
    )
    metadata = {"source": "commit-pointer"}
    versions = {"messages": "1"}
    admission = _accept_user_execution(
        saver,
        SESSION_ID,
        thread_id,
        checkpoint["channel_values"]["messages"][0],
        turn_id="turn-pointer",
    )
    with _execution_scope(admission):
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="同一个 canonical item", id="view-user")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-view"
    )
    with _execution_scope(admission):
        first_config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "cp-view-source",
                [user],
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
        assert kinds == [("acceptance", "item_bearing", 1)]


def test_read_rejects_broken_storage_commit_offset_chain(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="提交链完整性", id="user-broken-chain")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-broken-chain"
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "cp-broken-commit-chain",
                [user],
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
            saver.get_tuple(build_checkpoint_config(thread_id))


def test_read_rejects_storage_commit_with_incomplete_item_catalog(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="提交 catalog 不完整", id="user-broken-catalog")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-broken-catalog"
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "cp-broken-commit-catalog",
                [user],
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
            saver.get_tuple(build_checkpoint_config(thread_id))


def test_read_rejects_catalog_locator_that_does_not_match_canonical_jsonl(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="损坏 item locator", id="user-broken-item")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-broken-locator"
    )
    with _execution_scope(admission):
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint(
                "cp-broken-item-locator",
                [user],
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
            saver.get_tuple(build_checkpoint_config(thread_id))


def test_rollout_schema_exposes_all_authoritative_tables_and_core_constraints(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    storage.storage.initialize(SESSION_ID, thread_id=thread_id)
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
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="可恢复", id="u1")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-backup"
    )
    with _execution_scope(admission):
        config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", [user], counter=1),
            {"source": "backup-test"},
            {"messages": "1", "counter": "1"},
        )
        storage = _storage(sessions_dir)
        backup = storage.storage.backup_index(
            thread_id, session_id=SESSION_ID, destination=tmp_path / "index.bak"
        )
        index_path = storage.storage.index_path(SESSION_ID, thread_id=thread_id)
        index_path.write_bytes(b"corrupted")

        restored_snapshot = storage.storage.restore_index_backup_offline(
            thread_id, backup, session_id=SESSION_ID
        )
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
                "sha256": storage.storage._file_hash(quarantine / "index.sqlite"),
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
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    storage.storage.initialize(SESSION_ID, thread_id=thread_id)
    index_path = storage.storage.index_path(SESSION_ID, thread_id=thread_id)
    index_path.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(RuntimeError, match="recovery_required") as caught:
        storage.storage.backup_index(
            thread_id, session_id=SESSION_ID, destination=tmp_path / "index.bak"
        )

    assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_sqlite_restore_failure_does_not_leave_partial_temporary_index(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    storage.storage.initialize(SESSION_ID, thread_id=thread_id)
    broken_backup = tmp_path / "broken-index.bak"
    broken_backup.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(sqlite3.DatabaseError):
        storage.storage.restore_index_backup(thread_id, broken_backup, session_id=SESSION_ID)

    rollout_root = storage.storage.index_path(SESSION_ID, thread_id=thread_id).parent
    assert not tuple(rollout_root.glob(".index.sqlite.*.restore"))


def test_schema_migration_checksum_and_completion_are_authoritative(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    storage.storage.initialize(SESSION_ID, thread_id=thread_id)
    root = get_session_path_resolver(sessions_dir).resolve_session_node(SESSION_ID)
    with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
        connection.execute(
            "UPDATE schema_migrations SET migration_checksum = 'broken' WHERE from_version = 0"
        )
    with pytest.raises(RuntimeError, match="checksum"):
        storage.storage.initialize(SESSION_ID, thread_id=thread_id)


def test_schema_migration_runs_transactionally_and_keeps_backup(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    storage = _storage(sessions_dir)
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    storage.storage.initialize(SESSION_ID, thread_id=thread_id)
    target_version = storage_version.ROLLOUT_SCHEMA_VERSION + 1
    monkeypatch.setattr(
        storage_version,
        "ROLLOUT_SCHEMA_VERSION",
        target_version,
    )

    snapshot = storage.storage.migrate_schema(
        storage.storage.main_thread_id(SESSION_ID),
        session_id=SESSION_ID,
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
    thread_id = storage.storage.main_thread_id(SESSION_ID)
    storage.storage.initialize(SESSION_ID, thread_id=thread_id)
    target_version = storage_version.ROLLOUT_SCHEMA_VERSION + 1
    monkeypatch.setattr(
        storage_version,
        "ROLLOUT_SCHEMA_VERSION",
        target_version,
    )

    with pytest.raises(sqlite3.OperationalError):
        storage.storage.migrate_schema(
            storage.storage.main_thread_id(SESSION_ID),
            session_id=SESSION_ID,
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
