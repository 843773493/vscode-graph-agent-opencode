"""rollout storage 的锁、快照与只读值对象。

这些类型不承载业务规则；它们只定义跨 JSONL/SQLite 读写的生命周期与快照
边界，避免 RolloutStorage facade 继续膨胀。
"""

from __future__ import annotations

import asyncio
import errno
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, Self
from weakref import WeakValueDictionary

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)
from app.services.infrastructure.rollout_context.storage.guards import (
    pop_jsonl_guard,
    truncate_uncommitted_tail,
)


@dataclass(frozen=True, slots=True)
class RequestMessageGroup:
    """本次请求复用的已提交 canonical group 与权威 execution 归属。"""

    message_id: str
    turn_id: str | None
    execution_id: str
    items: tuple[CanonicalItemRecord, ...]


class MessageCodec(Protocol):
    """checkpoint 组装层注入的消息适配端口。

    canonical_items 只接受 locator 读出的完整已存消息组，仅复用 item_id/
    message_group_id；正文与 typed 元数据重新解析并完整比较，空序列分配新身份。
    """

    def message_id(self, message: object, index: int) -> str: ...

    def turn_id(self, message: object, current: str | None, message_id: str) -> str: ...

    def to_dict(self, message: object) -> dict[str, object]: ...

    def is_internal(self, message: object) -> bool: ...

    def message_role(self, message: object) -> str: ...

    def tool_calls(
        self, message: object | Mapping[str, object]
    ) -> list[Mapping[str, object]]: ...

    def model_call_id(self, message: object) -> str | None: ...

    def tool_message_model_call_id(
        self, message: object, preceding_messages: Sequence[object]
    ) -> str | None: ...

    def items_for_message(
        self,
        message: object,
        *,
        item_sequence: int,
        message_id: str,
        turn_id: str | None,
        execution_id: str | None = None,
        timestamp: str,
        model_call_id: str | None = None,
        canonical_items: Sequence[CanonicalItemRecord] = (),
    ) -> tuple[CanonicalItemRecord, ...]: ...

    def project_message(
        self, items: Sequence[CanonicalItemRecord],
    ) -> dict[str, object]: ...

    def visible_text(self, message: Mapping[str, object]) -> str: ...

    def reasoning_rows(self, message: Mapping[str, object]) -> list[dict[str, object]]: ...

    def projection_content(self, item: CanonicalItemRecord) -> str: ...

    def from_dict(self, value: object) -> object: ...

_ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS = 10.0
_ROLLOUT_FILE_LOCK_POLL_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class RolloutManifest:
    """从 SQLite 快照生成的内存状态，不是磁盘 manifest 文件。"""

    rollout_id: str
    checkpoint_ns: str
    active_branch_id: str
    committed_sequence: int
    latest_checkpoint_id: str | None
    projection_epoch: int
    rollout_format_version: int = storage_version.ROLLOUT_FORMAT_VERSION
    history_view_revision: int = 0
    source_overlay_epoch: int = 0


@dataclass(frozen=True, slots=True)
class RolloutTurnAnchor:
    """完整 Turn 锚点；来源 checkpoint 与截断前的框架状态 header 分别定位。"""

    turn_id: str
    view_id: str
    checkpoint_id: str
    state_header_checkpoint_id: str
    branch_id: str
    logical_turn_ordinal: int
    first_message_sequence: int
    user_message_sequence: int
    last_message_sequence: int
    final_message_sequence: int | None
    anchor_mode: str
    cutoff_message_sequence: int



@dataclass(frozen=True, slots=True)
class ContextTurnItemWindow:
    """同一 view 的 Turn ordinal window 对应的 canonical membership bounds。"""

    view_id: str
    first_logical_turn_ordinal: int
    last_logical_turn_ordinal: int
    view_exists: bool
    first_turn_exists: bool
    last_turn_exists: bool
    lower_logical_item_ordinal: int | None
    upper_logical_item_ordinal: int | None


@dataclass(frozen=True, slots=True)
class HistorySnapshotBounds:
    """同一读快照中的 history revision 与两条分页 lane 共用坐标上界。"""

    view_id: str
    history_view_revision: int
    upper_turn_ordinal: int | None
    upper_logical_item_ordinal: int | None


@dataclass(frozen=True, slots=True)
class InternalExecutionHistoryEntry:
    """一个有显式 display-only membership 的 internal execution item。"""

    execution_id: str
    job_id: str
    session_id: str
    thread_id: str
    outcome: str
    logical_item_ordinal: int
    elapsed_ms: int
    created_at: str
    updated_at: str
    item: CanonicalItemRecord


@dataclass(frozen=True, slots=True)
class InternalExecutionHistoryPage:
    """同一 snapshot 内的有界 internal execution item page。"""

    entries: tuple[InternalExecutionHistoryEntry, ...]
    has_more: bool


class RolloutNotMaterializedError(KeyError):
    """真实 owner 尚未执行首次 durable 写入，磁盘上没有 rollout。"""


_RolloutLockOwner = tuple[int, asyncio.Task[object] | None]


def _rollout_lock_owner() -> _RolloutLockOwner:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


@dataclass(slots=True)
class _RolloutLockHold:
    owner: _RolloutLockOwner
    exclusive: bool
    depth: int = 1
    handle: BinaryIO | None = None


@dataclass(eq=False, slots=True)
class _RolloutLockWaiter:
    owner: _RolloutLockOwner
    exclusive: bool
    loop: asyncio.AbstractEventLoop | None = None
    ready: asyncio.Event | None = None


class _RolloutPathLock:
    """规范路径的唯一 FIFO；同步线程与 asyncio Task 共用准入和持锁状态。"""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._queue: deque[_RolloutLockWaiter] = deque()
        self._holders: dict[_RolloutLockOwner, _RolloutLockHold] = {}

    def _reenter(
        self, owner: _RolloutLockOwner, *, path: Path, exclusive: bool,
    ) -> _RolloutLockHold | None:
        held = self._holders.get(owner)
        if held is not None:
            if exclusive and not held.exclusive:
                raise RuntimeError(f"rollout 共享锁不能升级为独占锁: path={path}")
            held.depth += 1
        return held

    def _admit(self, ticket: _RolloutLockWaiter) -> _RolloutLockHold | None:
        if self._queue[0] is not ticket or (
            self._holders
            and (
                ticket.exclusive or os.name == "nt"
                or any(holder.exclusive for holder in self._holders.values())
            )
        ):
            return None
        self._queue.popleft()
        held = _RolloutLockHold(ticket.owner, ticket.exclusive)
        self._holders[ticket.owner] = held
        return held

    def _notify(self) -> None:
        self._condition.notify_all()
        for ticket in self._queue:
            if ticket.loop is not None and ticket.ready is not None:
                ticket.loop.call_soon_threadsafe(ticket.ready.set)

    def _discard(self, ticket: _RolloutLockWaiter) -> None:
        if ticket in self._queue:
            self._queue.remove(ticket)
        self._notify()

    @staticmethod
    def _remaining(*, path: Path, exclusive: bool, deadline: float, timeout: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            label = "进程内写锁" if exclusive else "文件锁"
            raise TimeoutError(
                f"rollout {label}获取超时: path={path} timeout_seconds={timeout:g} stage=local_admission"
            )
        return remaining

    def _blocks_event_loop(self, ticket: _RolloutLockWaiter) -> bool:
        if ticket.owner[1] is None:
            return False
        thread_id = ticket.owner[0]
        if any(owner[0] == thread_id for owner in self._holders):
            return True
        for earlier in self._queue:
            if earlier is ticket:
                break
            if earlier.owner[0] == thread_id:
                return True
        return False

    def acquire(
        self, *, owner: _RolloutLockOwner, path: Path, exclusive: bool,
        deadline: float, timeout: float,
    ) -> _RolloutLockHold:
        with self._condition:
            held = self._reenter(owner, path=path, exclusive=exclusive)
            if held is not None:
                return held
            ticket = _RolloutLockWaiter(owner, exclusive)
            self._queue.append(ticket)
            try:
                while True:
                    remaining = self._remaining(
                        path=path, exclusive=exclusive, deadline=deadline, timeout=timeout,
                    )
                    held = self._admit(ticket)
                    if held is not None:
                        return held
                    if self._blocks_event_loop(ticket):
                        raise RuntimeError(
                            f"rollout 同步锁等待会阻塞持锁 Task 的事件循环；请使用 acquire_async: path={path}"
                        )
                    self._condition.wait(remaining)
            finally:
                self._discard(ticket)

    async def acquire_async(
        self, *, owner: _RolloutLockOwner, path: Path, exclusive: bool,
        deadline: float, timeout: float,
    ) -> _RolloutLockHold:
        ready = asyncio.Event()
        ticket = _RolloutLockWaiter(owner, exclusive, asyncio.get_running_loop(), ready)
        with self._condition:
            held = self._reenter(owner, path=path, exclusive=exclusive)
            if held is not None:
                return held
            self._queue.append(ticket)
        try:
            while True:
                with self._condition:
                    remaining = self._remaining(
                        path=path, exclusive=exclusive, deadline=deadline, timeout=timeout,
                    )
                    held = self._admit(ticket)
                    if held is not None:
                        return held
                    ready.clear()
                try:
                    async with asyncio.timeout(remaining):
                        await ready.wait()
                except TimeoutError:
                    self._remaining(
                        path=path, exclusive=exclusive, deadline=deadline, timeout=timeout,
                    )
        finally:
            with self._condition:
                self._discard(ticket)

    def release(self, held: _RolloutLockHold) -> None:
        with self._condition:
            held.depth -= 1
            if held.depth:
                return
        handle = held.handle
        try:
            if handle is not None:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                if handle is not None:
                    handle.close()
            finally:
                held.handle = None
                with self._condition:
                    del self._holders[held.owner]
                    self._notify()


_rollout_path_locks: WeakValueDictionary[Path, _RolloutPathLock] = WeakValueDictionary()
_rollout_path_locks_guard = threading.Lock()


def _rollout_path_lock(path: Path) -> _RolloutPathLock:
    with _rollout_path_locks_guard:
        admission = _rollout_path_locks.get(path)
        if admission is None:
            admission = _RolloutPathLock()
            _rollout_path_locks[path] = admission
        return admission


class _RolloutFileLock:
    """规范路径上的公平读写准入，加原有跨进程文件锁。"""

    def __init__(
        self,
        path: Path,
        *,
        exclusive: bool,
        timeout_seconds: float = _ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("rollout 文件锁 timeout_seconds 必须大于 0")
        self._path = Path(os.path.normcase(path.resolve()))
        self._exclusive = exclusive
        self._timeout_seconds = timeout_seconds
        self._admission: _RolloutPathLock | None = None
        self._held: _RolloutLockHold | None = None
        self._depth = 0

    def acquire(self) -> None:
        deadline = time.monotonic() + self._timeout_seconds
        admission = _rollout_path_lock(self._path)
        held = admission.acquire(
            owner=_rollout_lock_owner(), path=self._path, exclusive=self._exclusive,
            deadline=deadline, timeout=self._timeout_seconds,
        )
        try:
            self._bind_hold(admission, held)
            if held.handle is None:
                held.handle = self._acquire_handle(deadline)
        except BaseException:
            self._release_unfinished(admission, held)
            raise
        self._depth += 1

    async def acquire_async(self) -> None:
        deadline = time.monotonic() + self._timeout_seconds
        admission = _rollout_path_lock(self._path)
        held = await admission.acquire_async(
            owner=_rollout_lock_owner(), path=self._path, exclusive=self._exclusive,
            deadline=deadline, timeout=self._timeout_seconds,
        )
        try:
            self._bind_hold(admission, held)
            if held.handle is None:
                held.handle = await self._acquire_handle_async(deadline)
        except BaseException:
            self._release_unfinished(admission, held)
            raise
        self._depth += 1

    def _bind_hold(self, admission: _RolloutPathLock, held: _RolloutLockHold) -> None:
        if self._held is not None and self._held is not held:
            raise RuntimeError(f"同一 rollout 共享锁对象不能被并发复用: path={self._path}")
        self._admission, self._held = admission, held

    def _release_unfinished(self, admission: _RolloutPathLock, held: _RolloutLockHold) -> None:
        if self._depth == 0 and self._held is held:
            self._admission, self._held = None, None
        admission.release(held)

    def _open_handle(self) -> BinaryIO:
        if self._exclusive:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b" if self._exclusive else "rb")
        try:
            if os.name == "nt":
                # TODO: Windows CI 覆盖共享读锁；Windows 当前使用独占锁保证跨文件一致性。
                handle.seek(0, os.SEEK_END)
                if self._exclusive and handle.tell() == 0:
                    handle.write(b" ")
                    handle.flush()
                handle.seek(0)
            return handle
        except OSError as error:
            handle.close()
            raise RuntimeError(f"rollout 文件锁获取失败: {self._path}") from error
        except BaseException:
            handle.close()
            raise

    def _file_lock_delay(self, handle: BinaryIO, deadline: float) -> float | None:
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "rollout 文件锁获取超时: "
                f"path={self._path} timeout_seconds={self._timeout_seconds:g}"
            )
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    raise BlockingIOError from error
            else:
                import fcntl

                mode = fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH
                fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
        except BlockingIOError:
            return min(
                _ROLLOUT_FILE_LOCK_POLL_INTERVAL_SECONDS,
                max(0, deadline - time.monotonic()),
            )
        except OSError as error:
            raise RuntimeError(f"rollout 文件锁获取失败: {self._path}") from error
        return None

    def _acquire_handle(self, deadline: float) -> BinaryIO:
        handle = self._open_handle()
        try:
            while (delay := self._file_lock_delay(handle, deadline)) is not None:
                time.sleep(delay)
            return handle
        except BaseException:
            handle.close()
            raise

    async def _acquire_handle_async(self, deadline: float) -> BinaryIO:
        handle = self._open_handle()
        try:
            while (delay := self._file_lock_delay(handle, deadline)) is not None:
                await asyncio.sleep(delay)
            return handle
        except BaseException:
            handle.close()
            raise

    def release(self) -> None:
        admission, held = self._admission, self._held
        if admission is None or held is None:
            return
        self._depth -= 1
        if not self._depth:
            self._admission, self._held = None, None
        admission.release(held)

    async def __aenter__(self) -> Self:
        await self.acquire_async()
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._held is None or self._held.owner != _rollout_lock_owner():
            raise RuntimeError("不能释放其他执行流持有的 rollout 文件锁")
        self.release()


class _RolloutOperationLock(_RolloutFileLock):
    """同一进程可重入、跨进程独占的 rollout 写入锁。"""

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = _ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(path, exclusive=True, timeout_seconds=timeout_seconds)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        if self._held is None or self._held.owner != _rollout_lock_owner():
            raise RuntimeError("不能释放其他执行流持有的 rollout 写锁")
        self.release()


class _RolloutSQLiteConnection(sqlite3.Connection):
    """让 ``with connection`` 同时负责事务和连接生命周期。"""

    def __exit__(self, *args: object) -> bool | None:
        error = args[0] if args else None
        had_transaction = self.in_transaction
        guard = pop_jsonl_guard(self)
        try:
            result = super().__exit__(*args)
        finally:
            if (
                error is not None
                and guard is not None
                and not guard.commit_attempted
                and had_transaction
            ):
                truncate_uncommitted_tail(guard)
            self.close()
        return result


@dataclass(slots=True)
class RolloutReadSnapshot:
    session_id: str
    thread_id: str
    checkpoint_ns: str
    manifest: RolloutManifest
    connection: sqlite3.Connection
    file_lock: _RolloutFileLock
    _closed: bool = False

    @property
    def rollout_id(self) -> str:
        return self.manifest.rollout_id

    @property
    def projection_epoch(self) -> int:
        return self.manifest.projection_epoch

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.connection.rollback()
        finally:
            self.connection.close()
            self.file_lock.release()

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("rollout read snapshot 已关闭")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class RolloutCheckpointIndex:
    checkpoint_id: str
    checkpoint_ns: str
    commit_sequence: int
    message_sequence: int
    message_count: int
    parent_checkpoint_id: str | None
    view_id: str
    branch_id: str
    checkpoint_version: int
    checkpoint_timestamp: str
    checkpoint_json: str
    metadata_json: str
    versions_seen_type: str
    versions_seen_blob: bytes
    pending_sends_type: str
    pending_sends_blob: bytes


@dataclass(frozen=True, slots=True)
class RolloutPruningCandidate:
    """SQLite 中待逻辑裁剪的不可见 context view。"""

    checkpoint_id: str
    view_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RolloutPruningPlan:
    rollout_id: str
    committed_sequence: int
    candidates: tuple[RolloutPruningCandidate, ...]
