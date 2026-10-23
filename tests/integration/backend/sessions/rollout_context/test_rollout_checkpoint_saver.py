"""真实 Saver、checkpoint 和 canonical 存储的一致性合同。"""

from __future__ import annotations

import asyncio
import hashlib
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
from typing import BinaryIO

import portalocker
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple, empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.job_context import reset_current_job_id, set_current_job_id
from app.core.path_utils import catalog_provider_scope, get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
)
from app.domain.itemized.hashing import canonical_json_bytes, content_hash, sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import RuntimeNoticePayload
from app.prompting.factory import PromptSection, internal_message_factory
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.message_display import project_message_for_display
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
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.storage import (
    primitives as rollout_primitives,
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
from tests.support.rollout_output import commit_fixture_output
from tests.support.session_catalog_owner import fixture_catalog_scope
from tests.support.workspaces import prepare_default_test_workspace

SESSION_ID = "ses_019c205391bb7a4b81b6728df70f63e6"
LEGACY_SESSION_ID = "ses_019b9187b87f78c78ef242bbd6043bfc"


def _main_thread_node(sessions_dir: Path, session_id: str) -> Path:
    resolver = get_session_path_resolver(sessions_dir)
    return resolver.resolve_thread_node(session_id, resolver.main_thread_id(session_id))


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


@pytest.fixture
def start_model_call(frozen_provider_wire, frozen_provider_activation):
    """用正式封存和 ModelCall 登记启动已接受的 Turn，不跳过 open→active。"""
    def start(saver: RolloutCheckpointSaver, admission: ExecutionAdmission, user: HumanMessage) -> None:
        creation_key = f"model-start:{admission.execution_id}"
        prepared = saver.prepare_context_for_provider(
            admission.session_id, admission.thread_id,
            turn_id=admission.turn_id, execution_id=admission.execution_id,
            plan_creation_idempotency_key=creation_key,
            seal_idempotency_key=f"model-start-seal:{admission.execution_id}",
            request_messages=(user,),
            context_source_manager=ContextSourceManager(
                owner=ContextSourceOwnerKey(admission.session_id, admission.thread_id),
                control_state_port=saver,
            ),
            tool_set_supplier=lambda: ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
            **frozen_provider_wire("checkpoint-lifecycle-model", "chat_completions"),
            **frozen_provider_activation(saver, plan_creation_idempotency_key=creation_key),
        )
        snapshot = prepared["snapshot"]
        assert snapshot.seal_binding is not None and snapshot.model_call_id is not None
        saver.register_model_call(
            admission.session_id, thread_id=admission.thread_id,
            execution_id=admission.execution_id, model_call_id=snapshot.model_call_id,
            assembly_id=snapshot.assembly_id, attempt=1,
            provider="checkpoint-lifecycle-model", dispatch_state="dispatched",
        )

    return start


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
        name: checkpoint_id for name in checkpoint["channel_values"]
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
    lock_path = tmp_path / ".rollout.write.lock"
    original_lock = rollout_primitives.portalocker.lock

    def reject_nonblocking_lock(file: int, flags: int) -> None:
        if flags & portalocker.LOCK_NB:
            raise portalocker.AlreadyLocked("test lock is held")
        original_lock(file, flags)

    monkeypatch.setattr(rollout_primitives.portalocker, "lock", reject_nonblocking_lock)
    lock = _RolloutFileLock(
        lock_path,
        exclusive=True,
        timeout_seconds=0.01,
    )

    with pytest.raises(TimeoutError, match="rollout 文件锁获取超时"):
        lock.acquire()


@pytest.mark.skipif(os.name != "posix", reason="POSIX rollout 文件锁准入公平性")
def test_rollout_file_lock_writer_precedes_later_reader(tmp_path: Path) -> None:
    """已有 reader 释放后，排队 writer 必须先于新 reader 获准。"""
    lock_path = tmp_path / ".rollout.write.lock"
    lock_path.touch()
    earlier_reader = _RolloutFileLock(
        lock_path, exclusive=False, timeout_seconds=2,
    )
    earlier_reader.acquire()

    admission = earlier_reader._admission
    assert admission is not None
    condition = admission._condition
    original_wait = condition.wait
    writer_waiting = threading.Event()
    later_reader_progress = threading.Event()
    writer_admitted = threading.Event()
    reader_admitted = threading.Event()
    release_writer = threading.Event()
    release_later_reader = threading.Event()
    admission_order: list[str] = []
    order_lock = threading.Lock()

    def signal_waiting_thread(timeout: float | None = None) -> bool:
        name = threading.current_thread().name
        if name == "rollout-fairness-writer":
            writer_waiting.set()
        elif name == "rollout-fairness-later-reader":
            later_reader_progress.set()
        return original_wait(timeout)

    condition.wait = signal_waiting_thread

    def acquire_writer() -> None:
        lock = _RolloutFileLock(lock_path, exclusive=True, timeout_seconds=2)
        try:
            lock.acquire()
            with order_lock:
                admission_order.append("writer")
            writer_admitted.set()
            if not release_writer.wait(timeout=2):
                raise TimeoutError("测试未及时释放排队 writer")
        finally:
            lock.release()

    def acquire_later_reader() -> None:
        lock = _RolloutFileLock(lock_path, exclusive=False, timeout_seconds=2)
        try:
            lock.acquire()
            with order_lock:
                admission_order.append("later_reader")
            reader_admitted.set()
            later_reader_progress.set()
            if not release_later_reader.wait(timeout=2):
                raise TimeoutError("测试未及时释放后续 reader")
        finally:
            lock.release()

    writer = threading.Thread(
        target=acquire_writer, name="rollout-fairness-writer", daemon=True,
    )
    reader = threading.Thread(
        target=acquire_later_reader, name="rollout-fairness-later-reader", daemon=True,
    )
    writer.start()
    try:
        assert writer_waiting.wait(timeout=1), "writer 未进入锁准入等待"
        with condition:
            assert len(admission._queue) == 1, "writer ticket 未留在 FIFO 队列"

        reader.start()
        assert later_reader_progress.wait(timeout=1), "后续 reader 未开始准入"
        earlier_reader.release()
        assert writer_admitted.wait(timeout=1), "已排队 writer 未先获准"
        assert not reader_admitted.is_set(), "后续 reader 越过排队 writer 获准"
        release_writer.set()
        assert reader_admitted.wait(timeout=1), "writer 释放后后续 reader 未获准"
        with order_lock:
            assert admission_order == ["writer", "later_reader"]
    finally:
        release_writer.set()
        release_later_reader.set()
        earlier_reader.release()
        writer.join(timeout=2)
        if reader.ident is not None:
            reader.join(timeout=2)
        condition.wait = original_wait
        assert not writer.is_alive(), "writer 测试线程未退出"
        assert not reader.is_alive(), "reader 测试线程未退出"


@pytest.mark.skipif(os.name != "posix", reason="POSIX rollout 文件锁跨 facade 协调")
def test_rollout_file_lock_facades_coordinate_normalized_paths(tmp_path: Path) -> None:
    """规范路径相同的 FileLock 与 OperationLock 必须共享 FIFO 准入。"""
    lock_path = tmp_path / ".rollout.write.lock"
    lock_path.touch()
    alias_path = lock_path.parent / "." / lock_path.name
    symlink_parent = tmp_path / "same-directory-alias"
    symlink_parent.symlink_to(tmp_path, target_is_directory=True)
    symlink_lock_path = symlink_parent / lock_path.name
    earlier_reader = _RolloutFileLock(
        lock_path, exclusive=False, timeout_seconds=2,
    )
    earlier_reader.acquire()

    admission = earlier_reader._admission
    assert admission is not None
    condition = admission._condition
    original_wait = condition.wait
    writer_waiting = threading.Event()
    later_reader_progress = threading.Event()
    writer_admitted = threading.Event()
    reader_admitted = threading.Event()
    release_writer = threading.Event()
    release_later_reader = threading.Event()
    admission_order: list[str] = []
    order_lock = threading.Lock()

    def signal_waiting_thread(timeout: float | None = None) -> bool:
        name = threading.current_thread().name
        if name == "rollout-facade-writer":
            writer_waiting.set()
        elif name == "rollout-facade-reader":
            later_reader_progress.set()
        return original_wait(timeout)

    condition.wait = signal_waiting_thread

    def acquire_writer() -> None:
        lock = _RolloutOperationLock(alias_path, timeout_seconds=2)
        try:
            with lock:
                with order_lock:
                    admission_order.append("operation_writer")
                writer_admitted.set()
                if not release_writer.wait(timeout=2):
                    raise TimeoutError("测试未及时释放跨 facade writer")
        finally:
            if lock._held is not None:
                lock.__exit__(None, None, None)

    def acquire_later_reader() -> None:
        lock = _RolloutFileLock(symlink_lock_path, exclusive=False, timeout_seconds=2)
        try:
            lock.acquire()
            with order_lock:
                admission_order.append("later_reader")
            reader_admitted.set()
            later_reader_progress.set()
            if not release_later_reader.wait(timeout=2):
                raise TimeoutError("测试未及时释放跨 facade reader")
        finally:
            lock.release()

    writer = threading.Thread(
        target=acquire_writer, name="rollout-facade-writer", daemon=True,
    )
    reader = threading.Thread(
        target=acquire_later_reader, name="rollout-facade-reader", daemon=True,
    )
    writer.start()
    try:
        assert writer_waiting.wait(timeout=1), "OperationLock 未进入同路径准入等待"
        with condition:
            assert len(admission._queue) == 1, "跨 facade writer 未进入共享 FIFO 队列"

        reader.start()
        assert later_reader_progress.wait(timeout=1), "后续 FileLock reader 未开始准入"
        earlier_reader.release()
        assert writer_admitted.wait(timeout=1), "同路径 OperationLock writer 未先获准"
        assert not reader_admitted.is_set(), "后续 FileLock reader 越过 OperationLock writer"
        release_writer.set()
        assert reader_admitted.wait(timeout=1), "OperationLock writer 释放后 FileLock reader 未获准"
        with order_lock:
            assert admission_order == ["operation_writer", "later_reader"]
    finally:
        release_writer.set()
        release_later_reader.set()
        earlier_reader.release()
        writer.join(timeout=2)
        if reader.ident is not None:
            reader.join(timeout=2)
        condition.wait = original_wait
        assert not writer.is_alive(), "跨 facade writer 测试线程未退出"
        assert not reader.is_alive(), "跨 facade reader 测试线程未退出"


@pytest.mark.skipif(os.name != "posix", reason="POSIX rollout 文件锁读者 FIFO 准入")
def test_rollout_file_lock_queued_reader_precedes_later_writer(tmp_path: Path) -> None:
    """独占 holder 释放后，先排队的 shared reader 应先于后到 writer。"""
    lock_path = tmp_path / ".rollout.write.lock"
    holder = _RolloutFileLock(lock_path, exclusive=True, timeout_seconds=2)
    holder.acquire()

    admission = holder._admission
    assert admission is not None
    condition = admission._condition
    original_wait = condition.wait
    reader_waiting = threading.Event()
    writer_waiting = threading.Event()
    reader_admitted = threading.Event()
    writer_admitted = threading.Event()
    release_reader = threading.Event()
    release_writer = threading.Event()
    admission_order: list[str] = []
    order_lock = threading.Lock()

    def signal_waiting_thread(timeout: float | None = None) -> bool:
        name = threading.current_thread().name
        if name == "rollout-fifo-reader":
            reader_waiting.set()
        elif name == "rollout-fifo-writer":
            writer_waiting.set()
        return original_wait(timeout)

    condition.wait = signal_waiting_thread

    def acquire_reader() -> None:
        lock = _RolloutFileLock(lock_path, exclusive=False, timeout_seconds=2)
        try:
            lock.acquire()
            with order_lock:
                admission_order.append("reader")
            reader_admitted.set()
            if not release_reader.wait(timeout=2):
                raise TimeoutError("测试未及时释放先排队 reader")
        finally:
            lock.release()

    def acquire_writer() -> None:
        lock = _RolloutFileLock(lock_path, exclusive=True, timeout_seconds=2)
        try:
            lock.acquire()
            with order_lock:
                admission_order.append("writer")
            writer_admitted.set()
            if not release_writer.wait(timeout=2):
                raise TimeoutError("测试未及时释放后到 writer")
        finally:
            lock.release()

    reader = threading.Thread(
        target=acquire_reader, name="rollout-fifo-reader", daemon=True,
    )
    writer = threading.Thread(
        target=acquire_writer, name="rollout-fifo-writer", daemon=True,
    )
    reader.start()
    try:
        assert reader_waiting.wait(timeout=1), "shared reader 未进入锁准入等待"
        with condition:
            assert len(admission._queue) == 1, "shared reader ticket 未进入 FIFO 队列"

        writer.start()
        assert writer_waiting.wait(timeout=1), "后到 writer 未进入锁准入等待"
        with condition:
            assert len(admission._queue) == 2, "后到 writer ticket 未排在 reader 之后"

        holder.release()
        assert reader_admitted.wait(timeout=1), "先排队 shared reader 未先获准"
        assert not writer_admitted.is_set(), "后到 writer 越过先排队 reader 获准"
        release_reader.set()
        assert writer_admitted.wait(timeout=1), "shared reader 释放后 writer 未获准"
        with order_lock:
            assert admission_order == ["reader", "writer"]
    finally:
        release_reader.set()
        release_writer.set()
        holder.release()
        reader.join(timeout=2)
        if writer.ident is not None:
            writer.join(timeout=2)
        condition.wait = original_wait
        assert not reader.is_alive(), "FIFO reader 测试线程未退出"
        assert not writer.is_alive(), "FIFO writer 测试线程未退出"


async def _wait_for_rollout_lock_queue(lock: _RolloutFileLock, size: int) -> None:
    admission = lock._admission
    assert admission is not None
    async with asyncio.timeout(2):
        while True:
            with admission._condition:
                if len(admission._queue) == size:
                    return
            await asyncio.sleep(0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX rollout 共享锁的 Task 公平性")
async def test_rollout_async_reader_waits_behind_queued_thread_writer(tmp_path: Path) -> None:
    """同 loop 的不同 Task 不能借用前一 reader 的 hold 越过排队 writer。"""
    path = tmp_path / ".rollout.write.lock"
    path.touch()
    earlier = _RolloutFileLock(path, exclusive=False, timeout_seconds=2)
    await earlier.acquire_async()
    writer_ready = threading.Event()
    release_writer = threading.Event()
    order: list[str] = []

    def write() -> None:
        with _RolloutOperationLock(path, timeout_seconds=2):
            order.append("writer")
            writer_ready.set()
            assert release_writer.wait(timeout=2)

    async def read() -> None:
        async with _RolloutFileLock(path, exclusive=False, timeout_seconds=2):
            order.append("reader")

    later: asyncio.Task[None] | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(write)
        try:
            await _wait_for_rollout_lock_queue(earlier, 1)
            later = asyncio.create_task(read())
            await _wait_for_rollout_lock_queue(earlier, 2)
            assert not writer_ready.is_set() and not later.done()
            # 本 Task 能在等待期间继续运行并释放 earlier，证明没有阻塞事件循环。
            earlier.release()
            assert await asyncio.to_thread(writer_ready.wait, 1)
            assert not later.done()
            release_writer.set()
            await asyncio.wait_for(later, timeout=2)
            writer.result(timeout=1)
            assert order == ["writer", "reader"]
        finally:
            earlier.release()
            release_writer.set()
            if later is not None:
                later.cancel()
                await asyncio.gather(later, return_exceptions=True)


async def test_rollout_async_local_cancellation_removes_fifo_ticket(tmp_path: Path) -> None:
    path = tmp_path / ".rollout.write.lock"
    async with _RolloutOperationLock(path, timeout_seconds=2) as holder:
        admission = holder._admission
        assert admission is not None
        waiting = _RolloutFileLock(path, exclusive=False, timeout_seconds=2)
        task = asyncio.create_task(waiting.acquire_async())
        try:
            await _wait_for_rollout_lock_queue(holder, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not admission._queue
            assert len(admission._holders) == 1
            assert waiting._held is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with _RolloutOperationLock(path, timeout_seconds=0.1):
        pass
    assert not admission._holders


@pytest.mark.skipif(os.name != "posix", reason="真实 flock 等待与文件句柄取消清理")
async def test_rollout_async_file_cancellation_closes_fd_and_releases_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    path = tmp_path / ".rollout.write.lock"
    path.touch()
    opened = asyncio.Event()
    handles: list[BinaryIO] = []
    original_open = _RolloutFileLock._open_handle

    def record_open(lock: _RolloutFileLock) -> BinaryIO:
        handle = original_open(lock)
        handles.append(handle)
        opened.set()
        return handle

    monkeypatch.setattr(_RolloutFileLock, "_open_handle", record_open)
    with path.open("rb") as external:
        fcntl.flock(external.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        waiting = _RolloutFileLock(path, exclusive=False, timeout_seconds=2)
        task = asyncio.create_task(waiting.acquire_async())
        try:
            await asyncio.wait_for(opened.wait(), timeout=1)
            admission = waiting._admission
            assert admission is not None and len(admission._holders) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(handles) == 1 and handles[0].closed
            assert not admission._holders and not admission._queue
            assert waiting._held is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    with _RolloutOperationLock(path, timeout_seconds=0.1):
        pass


async def test_rollout_lock_reentry_is_bound_to_current_task(tmp_path: Path) -> None:
    path = tmp_path / ".rollout.write.lock"
    operation = _RolloutOperationLock(path, timeout_seconds=2)
    async with operation:
        held = operation._held
        assert held is not None
        with operation:
            async with _RolloutFileLock(path, exclusive=False) as reader:
                assert reader._held is held
                assert held.depth == 3
        assert held.depth == 1

        async def reject_synchronous_wait() -> None:
            with pytest.raises(RuntimeError, match="请使用 acquire_async"):
                _RolloutFileLock(path, exclusive=False).acquire()

        await asyncio.create_task(reject_synchronous_wait())
        child = asyncio.create_task(operation.acquire_async())
        try:
            await _wait_for_rollout_lock_queue(operation, 1)
            assert not child.done() and held.depth == 1
            child.cancel()
            with pytest.raises(asyncio.CancelledError):
                await child
        finally:
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
    async with _RolloutFileLock(path, exclusive=False):
        with pytest.raises(RuntimeError, match="共享锁不能升级为独占锁"):
            operation.acquire()
        with pytest.raises(RuntimeError, match="共享锁不能升级为独占锁"):
            await operation.acquire_async()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 本地排队与 flock 共用超时预算")
def test_rollout_local_and_file_wait_share_one_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, delay: float) -> None:
            self.now += delay

    path = tmp_path / ".rollout.write.lock"
    path.touch()
    holder = _RolloutFileLock(path, exclusive=False)
    holder.acquire()
    admission = holder._admission
    assert admission is not None
    clock = Clock()
    original_wait = admission._condition.wait
    queued = threading.Event()

    def signal_wait(timeout: float | None = None) -> bool:
        queued.set()
        return original_wait(timeout)

    monkeypatch.setattr(admission._condition, "wait", signal_wait)
    monkeypatch.setattr(rollout_primitives, "time", clock)
    with path.open("rb") as external, ThreadPoolExecutor(max_workers=1) as executor:
        fcntl.flock(external.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        waiter = _RolloutFileLock(path, exclusive=True, timeout_seconds=1)
        future = executor.submit(waiter.acquire)
        try:
            assert queued.wait(timeout=1)
            clock.now = 100.6
            holder.release()
            with pytest.raises(TimeoutError, match="rollout 文件锁获取超时"):
                future.result(timeout=1)
            assert clock.now == pytest.approx(101.0)
            assert waiter._held is None and not admission._holders
        finally:
            holder.release()


@pytest.mark.parametrize("wait_mode", ["sync", "async"])
async def test_rollout_local_admission_rejects_release_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait_mode: str,
) -> None:
    """holder 超预算后才唤醒 waiter，连本地准入也不得短暂成功。"""
    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

    path = tmp_path / ".rollout.write.lock"
    holder = _RolloutOperationLock(path, timeout_seconds=2)
    await holder.acquire_async()
    admission = holder._admission
    assert admission is not None
    original_admit = admission._admit
    admitted: list[rollout_primitives._RolloutLockHold] = []

    def record_admit(
        ticket: rollout_primitives._RolloutLockWaiter,
    ) -> rollout_primitives._RolloutLockHold | None:
        held = original_admit(ticket)
        if held is not None:
            admitted.append(held)
        return held

    clock = Clock()
    monkeypatch.setattr(admission, "_admit", record_admit)
    monkeypatch.setattr(rollout_primitives, "time", clock)
    waiter = _RolloutOperationLock(path, timeout_seconds=1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = (asyncio.get_running_loop().run_in_executor(executor, waiter.acquire)
                   if wait_mode == "sync" else asyncio.create_task(waiter.acquire_async()))
        try:
            await _wait_for_rollout_lock_queue(holder, 1)
            with admission._condition:
                clock.now = 101.1
            holder.release()
            with pytest.raises(TimeoutError, match="stage=local_admission"):
                await asyncio.wait_for(asyncio.shield(pending), timeout=2)
            assert admitted == [], "超期 waiter 不得先获准再在 flock 阶段报错"
            assert waiter._held is None
            assert not admission._holders and not admission._queue
        finally:
            holder.release()
            await asyncio.gather(pending, return_exceptions=True)


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
        with lock:
            assert lock._held is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX 文件解锁失败的真实句柄验证")
def test_rollout_operation_lock_releases_admission_when_file_unlock_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / ".rollout.write.lock"
    lock = _RolloutOperationLock(lock_path, timeout_seconds=0.1)
    lock.acquire()
    assert lock._held is not None
    handle = lock._held.handle
    assert handle is not None
    original_unlock = rollout_primitives.portalocker.unlock

    def fail_unlock(file: int) -> None:
        if file == handle.fileno():
            raise OSError("模拟解锁失败")
        original_unlock(file)

    with monkeypatch.context() as patch:
        patch.setattr(rollout_primitives.portalocker, "unlock", fail_unlock)
        with pytest.raises(OSError, match="模拟解锁失败"):
            lock.__exit__(None, None, None)
    assert handle.closed

    def acquire_after_failure() -> None:
        with _RolloutOperationLock(lock_path, timeout_seconds=0.1):
            pass

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(acquire_after_failure).result(timeout=1)


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

        def acquire_shared_from_another_thread() -> None:
            shared_lock = _RolloutFileLock(
                lock_path, exclusive=False, timeout_seconds=0.01,
            )
            try:
                shared_lock.acquire()
            finally:
                shared_lock.release()

        with (
            storage._lock(thread_id, "", session_id=SESSION_ID),
            storage._connect(thread_id, "", session_id=SESSION_ID) as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            assert storage.read_items(
                SESSION_ID, thread_id, connection=connection,
            ) == original_items
            assert connection.in_transaction
            # 同线程真实 snapshot 可以重入；关闭 snapshot 不得释放外层写锁。
            with (
                storage._lock(thread_id, "", session_id=SESSION_ID),
                storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot,
            ):
                assert storage.read_items(
                    SESSION_ID, thread_id, snapshot=snapshot,
                ) == original_items
            with ThreadPoolExecutor(max_workers=1) as executor:
                probe = executor.submit(acquire_shared_from_another_thread)
                with pytest.raises(TimeoutError, match="rollout 文件锁获取超时"):
                    probe.result(timeout=1)
            assert connection.in_transaction
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
        with storage.open_read_snapshot(SESSION_ID, thread_id) as snapshot:
            with (
                pytest.raises(RuntimeError, match="rollout 共享锁不能升级为独占锁"),
                storage._lock(thread_id, "", session_id=SESSION_ID),
            ):
                pytest.fail("共享读快照不能升级为写锁")
            assert storage.read_items(
                SESSION_ID, thread_id, snapshot=snapshot,
            ) == original_items
            with pytest.raises(ValueError, match="不能同时指定 snapshot 和 connection"):
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
            {"messages": "cp-invalid-tool-call"},
        )

        records = [
            json.loads(line)
            for line in (
                _main_thread_node(sessions_dir, SESSION_ID)
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
            {"messages": "cp-root"},
        )

        restored = saver.get_tuple(build_checkpoint_config(thread_id))
        assert restored is not None
        assert restored.parent_config is None
        root = _main_thread_node(sessions_dir, SESSION_ID)
        with sqlite3.connect(root / "rollout" / "index.sqlite") as connection:
            assert (
                connection.execute(
                    "SELECT parent_checkpoint_id FROM checkpoints WHERE checkpoint_id = 'cp-root'"
                ).fetchone()[0]
                is None
            )


def test_put_tool_result_phase_allows_queued_turn_before_storage_lock(
    tmp_path: Path,
    session_bundle_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.infrastructure.rollout_context.checkpoint.runtime import (
        langgraph_api,
    )

    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    model_call_id = "00000000-0000-4000-8000-000000000031"
    messages = _turn("turn-phase-first", "phase-first")
    messages[1] = messages[1].model_copy(update={"response_metadata": {"model_call_id": model_call_id}})
    admission = _accept_user_execution(saver, SESSION_ID, thread_id, messages[0], turn_id="turn-phase-first")
    phase_seen = threading.Event()
    release = threading.Event()
    observed = []

    def observe(operation_id, phase):
        observed.append((operation_id, phase))
        phase_seen.set()
        if not release.wait(3):
            raise TimeoutError("checkpoint 真实结果屏障未释放")

    monkeypatch.setattr(langgraph_api, "observe_phase_sync", observe)

    def put_first():
        with _execution_scope(admission):
            saver.put(
                build_checkpoint_config(thread_id), _checkpoint("cp-phase-first", messages),
                {"source": "phase-first"}, {"messages": "cp-phase-first"},
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(put_first)
        try:
            assert phase_seen.wait(1)
            queued = HumanMessage(content="queued root", id="user-phase-queued")
            acceptance = executor.submit(
                _accept_user_execution, saver, SESSION_ID, thread_id, queued,
                turn_id="turn-phase-queued",
            )
            assert acceptance.result(timeout=1).turn_id == "turn-phase-queued"
        finally:
            release.set()
        first.result(timeout=3)
    assert observed == [(f"{model_call_id}:tool-call:call-phase-first", "canonical.tool_result.before_commit")]
    history = saver._history_reader.load(
        SESSION_ID, thread_id=thread_id, request=TurnHistoryLoadRequest(),
    )
    assert [turn.turn_id for turn in history.items] == ["turn-phase-first", "turn-phase-queued"]


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


async def test_checkpoint_namespace_queries_do_not_cross_match(
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
            {"messages": "cp-ns-a"},
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
            {"messages": "cp-ns-b"},
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

    global_entries = list(saver.list(None))
    assert [(item.config["configurable"]["checkpoint_ns"], item.checkpoint["id"])
            for item in global_entries] == [("ns-b", "cp-ns-b"), ("ns-a", "cp-ns-a")]
    assert [item async for item in saver.alist(None)] == global_entries
    # LIMIT 只截断命中过滤器的结果，不能先丢掉较老的匹配项。
    assert list(saver.list(None, filter={"source": "namespace-a"}, limit=1)) == global_entries[1:]
    assert [item async for item in saver.alist(None, filter={"source": "namespace-a"}, limit=1)] == global_entries[1:]
    before = build_checkpoint_config(thread_id, checkpoint_ns="ns-b", checkpoint_id="cp-ns-b")
    assert list(saver.list(None, before=before)) == global_entries[1:]
    assert [item async for item in saver.alist(None, before=before)] == global_entries[1:]


@pytest.fixture
def checkpoint_listing(
    tmp_path: Path, session_bundle_factory,
) -> Iterator[tuple[RolloutCheckpointSaver, MutationIntentOwner, RunnableConfig]]:
    """通过真实 ingress 和 checkpoint 写入建立可枚举的 owner。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    with RolloutCheckpointSaver(sessions_dir) as saver:
        owner = saver.forks.source.resolve_main_thread_owner(SESSION_ID)
        config = build_checkpoint_config(owner.thread_id)
        user = HumanMessage(content="真实异步 checkpoint 枚举", id="user-alist")
        admission = _accept_user_execution(
            saver, SESSION_ID, owner.thread_id, user, turn_id="turn-alist",
        )
        with _execution_scope(admission):
            parent = config
            for index in range(3):
                parent = saver.put(
                    parent, _checkpoint(f"cp-alist-{index}", [user]),
                    {"source": "alist", "step": index}, {"messages": f"cp-alist-{index}"},
                )
        yield saver, owner, config


@pytest.mark.parametrize("all_owners", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_checkpoint_alist_awaits_holder_and_cleans_cancelled_admission(
    checkpoint_listing: tuple[RolloutCheckpointSaver, MutationIntentOwner, RunnableConfig],
    all_owners: bool, cancel: bool,
) -> None:
    saver, owner, config = checkpoint_listing

    async def collect() -> list[CheckpointTuple]:
        with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
            return [item async for item in saver.alist(None if all_owners else config)]

    holder = saver._storage.storage._lock(owner.thread_id, "", session_id=owner.session_id)
    await holder.acquire_async()
    admission = holder._admission
    assert admission is not None
    operation = asyncio.create_task(collect())
    try:
        await _wait_for_rollout_lock_queue(holder, 1)
        assert not operation.done()
        if cancel:
            operation.cancel("取消真实 alist 入口")
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert not admission._queue and len(admission._holders) == 1
        holder.release()
        items = await asyncio.wait_for(collect() if cancel else operation, timeout=2)
        assert [item.checkpoint["id"] for item in items] == [
            "cp-alist-2", "cp-alist-1", "cp-alist-0",
        ]
        assert items[0].checkpoint["channel_values"]["messages"][0].content == "真实异步 checkpoint 枚举"
        assert not admission._holders and not admission._queue
    finally:
        holder.release()
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.skipif(os.name != "posix", reason="真实 flock 竞争时 alist 必须让出事件循环")
async def test_checkpoint_alist_awaits_external_file_lock(
    checkpoint_listing: tuple[RolloutCheckpointSaver, MutationIntentOwner, RunnableConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    saver, owner, config = checkpoint_listing
    lock_path = saver._storage.storage.root(owner.session_id, thread_id=owner.thread_id).parent / ".rollout.write.lock"
    waiting = asyncio.Event()
    original_delay = _RolloutFileLock._file_lock_delay

    def observe_file_wait(lock: _RolloutFileLock, handle: BinaryIO, deadline: float) -> float | None:
        delay = original_delay(lock, handle, deadline)
        if delay is not None:
            waiting.set()
        return delay

    async def collect() -> list[CheckpointTuple]:
        with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
            return [item async for item in saver.alist(config, limit=1)]

    monkeypatch.setattr(_RolloutFileLock, "_file_lock_delay", observe_file_wait)
    with lock_path.open("rb") as external:
        fcntl.flock(external.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        operation = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(waiting.wait(), timeout=1)
            assert not operation.done(), "alist 在外部锁释放前不应同步阻塞至超时"
            fcntl.flock(external.fileno(), fcntl.LOCK_UN)
            items = await asyncio.wait_for(operation, timeout=2)
            assert [item.checkpoint["id"] for item in items] == ["cp-alist-2"]
        finally:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("all_owners", [False, True])
@pytest.mark.parametrize("use_before", [False, True])
async def test_checkpoint_alist_preserves_before_filter_and_limit(
    checkpoint_listing: tuple[RolloutCheckpointSaver, MutationIntentOwner, RunnableConfig],
    all_owners: bool,
    use_before: bool,
) -> None:
    saver, owner, config = checkpoint_listing
    before = build_checkpoint_config(owner.thread_id, checkpoint_id="cp-alist-2") if use_before else None
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        query = None if all_owners else config
        expected = list(saver.list(query, before=before, filter={"step": 1}, limit=1))
        actual = [item async for item in saver.alist(query, before=before, filter={"step": 1}, limit=1)]
    assert [item.checkpoint["id"] for item in expected] == ["cp-alist-1"]
    assert actual == expected


async def test_checkpoint_alist_reenters_fifo_between_yields(
    checkpoint_listing: tuple[RolloutCheckpointSaver, MutationIntentOwner, RunnableConfig],
) -> None:
    """消费方暂停枚举时不持锁，下一项仍须经过真实异步 FIFO。"""
    saver, owner, config = checkpoint_listing
    holder = saver._storage.storage._lock(owner.thread_id, "", session_id=owner.session_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        stream = saver.alist(config)
        operation: asyncio.Task[CheckpointTuple] | None = None
        try:
            assert (await anext(stream)).checkpoint["id"] == "cp-alist-2"
            await holder.acquire_async()
            operation = asyncio.create_task(anext(stream))
            await _wait_for_rollout_lock_queue(holder, 1)
            assert not operation.done()
            holder.release()
            assert (await asyncio.wait_for(operation, timeout=2)).checkpoint["id"] == "cp-alist-1"
            assert [item.checkpoint["id"] async for item in stream] == ["cp-alist-0"]
        finally:
            holder.release()
            if operation is not None:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            await stream.aclose()


def test_unmaterialized_main_item_projections_do_not_create_rollout(
    tmp_path: Path, session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    with RolloutCheckpointSaver(sessions_dir) as saver:
        storage = saver._storage.storage
        owner = saver.forks.source.resolve_main_thread_owner(SESSION_ID)
        root = storage.root(SESSION_ID, thread_id=owner.thread_id)
        lock = root.parent / ".rollout.write.lock"
        assert not root.exists() and not lock.exists()
        assert storage.read_item_projections(SESSION_ID, owner.thread_id) == []
        with pytest.raises(KeyError, match="item projection 缺少请求的 canonical item: item-missing"):
            storage.read_item_projections(SESSION_ID, owner.thread_id, item_ids=("item-missing",))
        assert not root.exists() and not lock.exists()


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
                {"messages": "cp-child"},
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
            {"messages": "cp-parent"},
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
        _main_thread_node(sessions_dir, SESSION_ID)
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
            {"messages": "cp-existing"},
        )

        def fail_initialize(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("已有 rollout 的 checkpoint 读取不应重新初始化")

        monkeypatch.setattr(storage.storage, "initialize", fail_initialize)
        restored = saver.get_tuple(config)

        assert restored is not None
        assert restored.checkpoint["id"] == "cp-existing"


@pytest.mark.asyncio
async def test_delete_legacy_rollout_does_not_initialize_removed_layout(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, LEGACY_SESSION_ID)
    rollout_root = (
        _main_thread_node(sessions_dir, LEGACY_SESSION_ID)
        / "rollout"
    )
    rollout_root.mkdir(parents=True)
    (rollout_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    (rollout_root / "segment-001.jsonl").write_text("{}\n", encoding="utf-8")
    (rollout_root / "index.sqlite").touch()

    storage = _storage(sessions_dir)
    thread_id = storage.storage.main_thread_id(LEGACY_SESSION_ID)

    assert storage.forks.pinned_fork_children(LEGACY_SESSION_ID) == ()
    await storage.forks.delete_thread(LEGACY_SESSION_ID, thread_id=thread_id, attachment_store=None)

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
            {"messages": "cp-1"},
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
            {"messages": "cp-2"},
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


def test_history_reads_accepted_turn_from_active_successor_before_next_checkpoint(
    tmp_path: Path,
    session_bundle_factory,
    start_model_call,
) -> None:
    """已接受的新 Turn 属于 active history，不修改前一冻结 checkpoint。"""
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    first = _turn("turn-1", "001")
    first_admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, first[0], turn_id="turn-1"
    )
    with _execution_scope(first_admission):
        start_model_call(saver, first_admission, first[0])
        commit_fixture_output(saver, messages=first)
        checkpoint_config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", first),
            {"source": "active-history-test"},
            {"messages": "cp-1"},
        )
    context_reader = RolloutContextReader(_storage(sessions_dir))
    reader = RolloutHistoryReader(context_reader)
    initial = reader.load(
        SESSION_ID, thread_id, TurnHistoryLoadRequest(direction="tail", turns=1)
    )
    assert [item.turn_id for item in initial.items] == ["turn-1"]

    second = _turn("turn-2", "002")
    _accept_user_execution(
        saver, SESSION_ID, thread_id, second[0], turn_id="turn-2"
    )
    with context_reader.open_snapshot(SESSION_ID, thread_id) as snapshot:
        checkpoint = context_reader.latest_checkpoint(snapshot)
        assert checkpoint is not None and checkpoint.checkpoint_id == "cp-1"
        active_view_id = snapshot.connection.execute(
            "SELECT head_view_id FROM branches WHERE branch_id = ?",
            (snapshot.manifest.active_branch_id,),
        ).fetchone()[0]
        assert active_view_id != checkpoint.view_id
        assert snapshot.connection.execute(
            "SELECT COUNT(*) FROM context_view_turns WHERE view_id = ? AND turn_id = ?",
            (active_view_id, "turn-2"),
        ).fetchone()[0] == 1
        assert snapshot.connection.execute(
            "SELECT COUNT(*) FROM context_view_turns WHERE view_id = ? AND turn_id = ?",
            (checkpoint.view_id, "turn-2"),
        ).fetchone()[0] == 0
        frozen_chain = context_reader.resolve_chain(snapshot, checkpoint.message_sequence)
        assert context_reader.context_view_id(frozen_chain) == checkpoint.view_id
        assert context_reader.context_turn_count(snapshot, frozen_chain) == 1

    for history_reader in (reader, RolloutHistoryReader(context_reader)):
        detail = history_reader.load(
            SESSION_ID,
            thread_id,
            TurnHistoryLoadRequest(turn_ids=["turn-2"], include=["user"]),
        )
        assert [item.turn_id for item in detail.items] == ["turn-2"]
        assert detail.items[0].ordinal == 2
        assert detail.items[0].user_messages[0].content == second[0].content
        latest = history_reader.load(
            SESSION_ID, thread_id, TurnHistoryLoadRequest(direction="tail", turns=1)
        )
        assert [item.turn_id for item in latest.items] == ["turn-2"]
    with _execution_scope(first_admission):
        restored = saver.get_tuple(checkpoint_config)
    assert restored is not None
    assert [message.id for message in restored.checkpoint["channel_values"]["messages"]] == [
        message.id for message in first
    ]


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
    start_model_call,
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
        start_model_call(saver, admission, messages[0])
        commit_fixture_output(saver, messages=messages)
        checkpoint_config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", messages, counter=3, task_state=None),
            {"source": "unit", "step": 1},
            {"messages": "cp-1", "counter": "cp-1", "task_state": "cp-1"},
        )

        restored = saver.get_tuple(checkpoint_config)
        assert restored is not None
        assert [
            message.id for message in restored.checkpoint["channel_values"]["messages"]
        ] == [message.id for message in messages]
        assert restored.checkpoint["channel_values"]["counter"] == 3
        assert restored.checkpoint["channel_values"]["task_state"] is None

        rollout_root = (
            _main_thread_node(sessions_dir, SESSION_ID)
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
            terminal = connection.execute(
                "SELECT commit_id,commit_mode,outcome FROM storage_commits "
                "WHERE commit_kind='terminal_convergence'",
            ).fetchall()
            assert len(terminal) == 1 and terminal[0][1:] == ("item_bearing", "completed")
            assert connection.execute(
                "SELECT item.commit_id,execution.outcome FROM turn_records turn "
                "JOIN item_catalog item ON item.item_id=turn.final_item_id "
                "JOIN executions execution ON execution.execution_id=turn.last_execution_id "
                "WHERE turn.turn_id='turn-1'",
            ).fetchone() == (terminal[0][0], "completed")
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
            {"messages": "cp-failed"},
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
            {"messages": "cp-cancelled"},
        )

        def turn_status() -> str:
            with sqlite3.connect(
                _main_thread_node(sessions_dir, SESSION_ID)
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
    start_model_call,
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
        start_model_call(saver, admission, user)
        saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", [user]),
            {"source": "test"},
            {"messages": "cp-1"},
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
            {"messages": "cp-before-reminder-failure"},
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


def test_structured_runtime_notice_checkpoint_cold_restore_preserves_private_and_safe_parts(
    tmp_path: Path, session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    prepared = internal_message_factory.build(
        kind="generated_session_result", control="处理返回的分支结果。",
        sections=(
            PromptSection("control_context", {}),
            PromptSection("generated_session_result", "仅供模型的私有分支结果"),
        ),
        display_content="分支任务已完成。",
    )
    message = HumanMessage(
        content=prepared.content, id="structured-notice-checkpoint",
        response_metadata={
            "message_id": "structured-notice-checkpoint",
            "created_at": "2026-10-19T00:00:00+00:00",
            "message_metadata": prepared.metadata,
        },
    )
    intent = InternalExecutionIntent(
        message_id=message.id, execution_id="execution-structured-notice",
        admission_idempotency_key="admission-structured-notice",
    )
    with RolloutCheckpointSaver(sessions_dir) as saver:
        thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
        admission = saver.admit_internal_execution(
            SESSION_ID, thread_id=thread_id, job_id="job-structured-notice",
            intent=intent, message=message,
        )
        with _execution_scope(admission):
            config = saver.put(
                build_checkpoint_config(thread_id),
                _checkpoint("checkpoint-structured-notice", [message]),
                {"source": "runtime-notice-roundtrip"}, {"messages": "checkpoint-structured-notice"},
            )
        (original,) = saver._storage.storage.read_items(
            SESSION_ID, thread_id, item_ids=(intent.notice_item_id,),
        )
        original_bytes = canonical_json_bytes(original.to_dict())
    with RolloutCheckpointSaver(sessions_dir) as cold, _execution_scope(admission):
        restored = cold.get_tuple(config)
        assert restored is not None
        (restored_message,) = restored.checkpoint["channel_values"]["messages"]
        assert isinstance(restored_message, HumanMessage)
        assert restored_message.content == prepared.content
        assert "仅供模型的私有分支结果" not in canonical_json_bytes(restored_message.response_metadata).decode()
        display = project_message_for_display(restored_message.content, restored_message.response_metadata)
        assert display.content == "分支任务已完成。"
        (notice,) = cold._storage.storage.read_items(
            SESSION_ID, thread_id, item_ids=(intent.notice_item_id,),
        )
        payload = RuntimeNoticePayload.from_dict(notice.payload)
        assert payload.prompt == prepared.content
        assert payload.display_part.text == display.content
        assert canonical_json_bytes(notice.to_dict()) == original_bytes
        assert notice.content_hash == content_hash("structured_content", notice.payload)
        assert notice.turn_id is None and notice.turn_scope == "pending_next_turn"
        assert cold.execution_admission(SESSION_ID, thread_id=thread_id, execution_id=intent.execution_id) == admission


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
            {"messages": "cp-legacy"},
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
    start_model_call,
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
        start_model_call(saver, first_admission, first[0])
        commit_fixture_output(saver, messages=first)
        first_config = saver.put(
            build_checkpoint_config(thread_id),
            _checkpoint("cp-1", first),
            {"source": "unit"},
            {"messages": "cp-1"},
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
            {"messages": "cp-2"},
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
            {"messages": "cp-3"},
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
            {"messages": "cp-1", "counter": "cp-1", "optional": "cp-1"},
        )
        saver.put_writes(config, [("counter", 1)], "task-1", "node-a")
        restored = saver.get_tuple(config)
        assert restored is not None
        assert restored.checkpoint["channel_values"]["counter"] == 0
        assert restored.checkpoint["channel_values"]["optional"] is None
        assert restored.pending_writes == [("task-1", "counter", 1)]


@pytest.fixture
def delta_history_case(
    tmp_path: Path, session_bundle_factory,
) -> tuple[RolloutCheckpointSaver, ExecutionAdmission, RunnableConfig, Path]:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    thread_id = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="恢复增量通道", id="delta-user")
    admission = _accept_user_execution(
        saver, SESSION_ID, thread_id, user, turn_id="turn-delta"
    )

    def put(parent: RunnableConfig, checkpoint_id: str, **values: object) -> RunnableConfig:
        checkpoint = _checkpoint(checkpoint_id, [user], **values)
        # absent 保留版本，不能因此误当作已经持久化的通道 seed。
        checkpoint["channel_versions"].update({
            channel: checkpoint_id
            for channel in ("files", "optional", "zero", "false", "mapping", "late", "never")
        })
        return saver.put(parent, checkpoint, {"source": "unit"}, checkpoint["channel_versions"])

    with _execution_scope(admission):
        root = put(
            build_checkpoint_config(thread_id), "delta-root",
            files=[], optional="旧值", zero=0, false=False, mapping={},
        )
        saver.put_writes(root, [("files", "root-a"), ("files", "root-b"), ("never", "root-never")], "root-task", "a")
        saver.put_writes(root, [("optional", "旧seed后的写入")], "root-optional", "b")
        middle = put(root, "delta-middle", optional=None, late=[])
        saver.put_writes(middle, [("files", "middle-a"), ("files", "middle-b"), ("optional", "middle-null"), ("late", "middle-late"), ("never", "middle-never")], "middle-task", "a")
        # 旁支处于两个祖先之间的提交时间，恢复只能沿 target 的真实父链。
        side = put(root, "delta-side", files=["旁支seed"], optional="旁支值")
        saver.put_writes(side, [("files", "旁支写入")], "side-task", "a")
        target = put(middle, "delta-target", files=["目标seed不属于父链"])
        saver.put_writes(target, [("files", "目标写入"), ("never", "目标never")], "target-task", "a")

    # 另一个 namespace 的较晚提交也不能加入主 namespace 的历史。
    other_user = HumanMessage(content="独立命名空间", id="delta-other-user")
    other = _accept_user_execution(
        saver, SESSION_ID, thread_id, other_user,
        turn_id="turn-delta-other", checkpoint_ns="other",
    )
    with _execution_scope(other):
        other_checkpoint = _checkpoint("delta-other", [other_user], files=["其它命名空间"])
        saver.put(
            build_checkpoint_config(thread_id, checkpoint_ns="other"), other_checkpoint,
            {"source": "unit"}, other_checkpoint["channel_versions"],
        )
    index_path = saver._storage.storage.root(SESSION_ID, thread_id=thread_id) / "index.sqlite"
    return saver, admission, target, index_path


@pytest.mark.asyncio
async def test_delta_channel_history_restores_cold_parent_path_without_materializing_messages(
    delta_history_case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, admission, target, _index_path = delta_history_case
    cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
    channels = ["files", "optional", "zero", "false", "mapping", "late", "never"]
    expected = {
        "files": {"seed": [], "writes": [("root-task", "files", "root-a"), ("root-task", "files", "root-b"), ("middle-task", "files", "middle-a"), ("middle-task", "files", "middle-b")]},
        "optional": {"seed": None, "writes": [("middle-task", "optional", "middle-null")]},
        "zero": {"seed": 0, "writes": []},
        "false": {"seed": False, "writes": []},
        "mapping": {"seed": {}, "writes": []},
        "late": {"seed": [], "writes": [("middle-task", "late", "middle-late")]},
        "never": {"writes": [("root-task", "never", "root-never"), ("middle-task", "never", "middle-never")]},
    }
    snapshots = []
    original_open = cold._storage.storage.open_read_snapshot

    def open_once(*args, **kwargs):
        snapshot = original_open(*args, **kwargs)
        snapshots.append(snapshot)
        return snapshot

    def forbid_messages(*args, **kwargs):
        raise AssertionError("Delta恢复不得物化未请求的messages")

    monkeypatch.setattr(cold._storage.storage, "open_read_snapshot", open_once)
    monkeypatch.setattr(cold._storage.checkpoints, "_messages_for_view", forbid_messages)
    with _execution_scope(admission):
        assert cold.get_delta_channel_history(config=target, channels=channels) == expected
        assert await cold.aget_delta_channel_history(config=target, channels=channels) == expected
        assert len(snapshots) == 2
        assert cold.get_delta_channel_history(
            config=build_checkpoint_config(admission.thread_id), channels=channels,
        ) == expected
        assert cold.get_delta_channel_history(
            config=build_checkpoint_config(admission.thread_id, checkpoint_id="delta-root"), channels=channels,
        ) == {channel: {"writes": []} for channel in channels}
        assert await cold.aget_delta_channel_history(
            config=build_checkpoint_config(admission.thread_id, checkpoint_id="missing"), channels=channels,
        ) == {channel: {"writes": []} for channel in channels}
        assert cold.get_delta_channel_history(config={}, channels=[]) == {}
        assert await cold.aget_delta_channel_history(config={}, channels=[]) == {}
        with pytest.raises(ValueError, match="history-projection-unavailable"):
            cold.get_delta_channel_history(
                config={"configurable": {**target["configurable"], "history_view_id": "missing-view"}},
                channels=channels,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(("corruption", "message"), [
    ("pending_length", "pending_writes.value_length"),
    ("channel_hash", "checkpoint channel BLOB"),
    ("channel_length", "checkpoint channel BLOB"),
    ("absent_storage_kind", "checkpoint absent channel 存储状态非法"),
    ("absent_payload", "checkpoint absent channel 存储状态非法"),
    ("missing_parent", "parent lineage 缺少"),
    ("cycle", "parent lineage 存在循环"),
    ("messages_pointer", "checkpoint messages selection"),
])
async def test_delta_channel_history_rejects_corrupt_requested_history_without_writes(
    delta_history_case, corruption: str, message: str,
) -> None:
    saver, admission, target, index_path = delta_history_case
    statements = {
        "pending_length": "UPDATE pending_writes SET value_length=value_length+1 WHERE checkpoint_id='delta-middle' AND channel='files'",
        "channel_hash": "UPDATE checkpoint_state_values SET value_hash='damaged' WHERE value_id=(SELECT value_id FROM checkpoint_channels WHERE checkpoint_id='delta-root' AND channel_name='files')",
        "channel_length": "UPDATE checkpoint_state_values SET value_length=value_length+1 WHERE value_id=(SELECT value_id FROM checkpoint_channels WHERE checkpoint_id='delta-root' AND channel_name='files')",
        "absent_storage_kind": "UPDATE checkpoint_channels SET storage_kind='rollout_view' WHERE checkpoint_id='delta-middle' AND channel_name='files'",
        "absent_payload": "UPDATE checkpoint_channels SET value_id=1 WHERE checkpoint_id='delta-middle' AND channel_name='files'",
        "missing_parent": "UPDATE checkpoints SET parent_checkpoint_id='missing-parent' WHERE checkpoint_id='delta-middle'",
        "cycle": "UPDATE checkpoints SET parent_checkpoint_id='delta-target' WHERE checkpoint_id='delta-middle'",
        "messages_pointer": "UPDATE checkpoint_channels SET context_view_id=(SELECT view_id FROM checkpoints WHERE checkpoint_id='delta-root') WHERE checkpoint_id='delta-middle' AND channel_name='messages'",
    }
    with sqlite3.connect(index_path) as connection:
        connection.execute(statements[corruption])
        connection.commit()
    before_database = index_path.read_bytes()
    jsonl_path = index_path.with_name("rollout.jsonl")
    before_jsonl = jsonl_path.read_bytes()
    with _execution_scope(admission):
        with pytest.raises(RuntimeError, match=message):
            saver.get_delta_channel_history(config=target, channels=["files"])
        with pytest.raises(RuntimeError, match=message):
            await saver.aget_delta_channel_history(config=target, channels=["files"])
    assert index_path.read_bytes() == before_database
    assert jsonl_path.read_bytes() == before_jsonl


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
            {"messages": "cp-1"},
        )
        saver.put_writes(config, [("counter", 1)], "task-1", "node-a")
        root = _main_thread_node(sessions_dir, SESSION_ID)
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
            {"messages": "cp-1"},
        )
        root = (
            _main_thread_node(sessions_dir, SESSION_ID)
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
        _main_thread_node(sessions_dir, SESSION_ID)
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
                config, checkpoint, {"source": "commit-window"}, {"messages": checkpoint["channel_versions"]["messages"]}
            )

        rollout_path = (
            _main_thread_node(sessions_dir, SESSION_ID)
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
            {"messages": checkpoint["channel_versions"]["messages"]},
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
                    {"messages": f"cp-concurrent-{index}"},
                )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(append, range(4)))

    root = _main_thread_node(sessions_dir, SESSION_ID)
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
    versions = {"messages": checkpoint["channel_versions"]["messages"]}
    admission = _accept_user_execution(
        saver,
        SESSION_ID,
        thread_id,
        checkpoint["channel_values"]["messages"][0],
        turn_id="turn-idempotent",
    )
    with _execution_scope(admission):
        first_config = saver.put(config, checkpoint, metadata, versions)
        root = _main_thread_node(sessions_dir, SESSION_ID)
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
    versions = {"messages": checkpoint["channel_versions"]["messages"]}
    admission = _accept_user_execution(
        saver,
        SESSION_ID,
        thread_id,
        checkpoint["channel_values"]["messages"][0],
        turn_id="turn-pointer",
    )
    with _execution_scope(admission):
        saver.put(config, checkpoint, metadata, versions)
        root = _main_thread_node(sessions_dir, SESSION_ID)
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
            {"messages": "cp-view-source"},
        )
        second_config = saver.put(
            first_config,
            _checkpoint(
                "cp-view-only",
                [HumanMessage(content="同一个 canonical item", id="view-user")],
            ),
            {"source": "view-only"},
            {"messages": "cp-view-only"},
        )

        root = _main_thread_node(sessions_dir, SESSION_ID)
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
            {"messages": "cp-broken-commit-chain"},
        )
        root = _main_thread_node(sessions_dir, SESSION_ID)
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
            {"messages": "cp-broken-commit-catalog"},
        )
        root = _main_thread_node(sessions_dir, SESSION_ID)
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
            {"messages": "cp-broken-item-locator"},
        )
        root = _main_thread_node(sessions_dir, SESSION_ID)
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
    root = _main_thread_node(sessions_dir, SESSION_ID)
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
            {"messages": "cp-1", "counter": "cp-1"},
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
        assert json.dumps(str(backup.parent))[1:-1] not in json.dumps(restore_manifest)
        assert restore_manifest["source_sha256"] == restore_manifest["installed_sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
        assert restore_manifest["quarantined"] == [
            {
                "name": "index.sqlite",
                "sha256": hashlib.sha256((quarantine / "index.sqlite").read_bytes()).hexdigest(),
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
    root = _main_thread_node(sessions_dir, SESSION_ID)
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

    storage.storage.migrate_schema(
        storage.storage.main_thread_id(SESSION_ID),
        session_id=SESSION_ID,
        to_version=target_version,
        migration_name="add_migration_probe_index",
        migration_sql="CREATE INDEX migration_probe ON messages(message_id)",
    )
    with storage.storage.validate_index(SESSION_ID, thread_id) as snapshot:
        assert snapshot.manifest.rollout_id.startswith("rollout-")
    root = _main_thread_node(sessions_dir, SESSION_ID)
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

    root = _main_thread_node(sessions_dir, SESSION_ID)
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
