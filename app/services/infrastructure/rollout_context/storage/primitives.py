"""rollout storage 的锁、快照与只读值对象。

这些类型不承载业务规则；它们只定义跨 JSONL/SQLite 读写的生命周期与快照
边界，避免 RolloutStorage facade 继续膨胀。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, Self
from weakref import WeakValueDictionary

import portalocker

from app.core.atomic_fs import native_io_path
from app.core.sqlite_state import sqlite_file_uri, validate_sqlite_path_budget
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


logger = logging.getLogger(__name__)


def _storage_failure(
    path: Path, reason: str, stage: str, error: BaseException | None = None,
    *, artifact: str,
) -> str:
    detail = f"{reason}: artifact={artifact} stage={stage}"
    if error is not None:
        detail += f" error_type={type(error).__name__}"
        for name in ("errno", "winerror"):
            value = getattr(error, name, None)
            if value is not None:
                detail += f" {name}={value}"
    logger.error("%s; path=%s", detail, path, exc_info=error)
    return detail


@contextmanager
def _storage_io(path: Path, reason: str, stage: str, *, artifact: str) -> Iterator[None]:
    """只在原生调用边界投影错误；原异常与位置保留在日志和 cause。"""
    try:
        yield
    except OSError as error:
        detail = _storage_failure(path, reason, stage, error, artifact=artifact)
        winerror = getattr(error, "winerror", None)
        if winerror is not None:
            raise type(error)(error.errno, detail, None, winerror) from error
        raise type(error)(error.errno, detail) from error
    except portalocker.LockException as error:
        raise type(error)(_storage_failure(path, reason, stage, error, artifact=artifact)) from error


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
                raise RuntimeError(_storage_failure(path, "rollout 共享锁不能升级为独占锁", "reentry", artifact="rollout_lock"))
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
                _storage_failure(path, f"rollout {label}获取超时 timeout_seconds={timeout:g}", "local_admission", artifact="rollout_lock")
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
                            _storage_failure(path, "rollout 同步锁等待会阻塞持锁 Task 的事件循环；请使用 acquire_async", "local_admission", artifact="rollout_lock")
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
                portalocker.unlock(handle.fileno())
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
        with _storage_io(path, "rollout 文件锁 I/O 失败", "resolve", artifact="rollout_lock"):
            try:
                resolved = path.resolve()
            except RuntimeError as error:
                # Python 3.12 的符号链接环以 RuntimeError 报错，仍属于原生解析失败。
                raise RuntimeError(_storage_failure(path, "rollout 文件锁路径解析失败", "resolve", error, artifact="rollout_lock")) from error
            self._path = Path(os.path.normcase(resolved))
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
            raise RuntimeError(_storage_failure(self._path, "同一 rollout 共享锁对象不能被并发复用", "bind", artifact="rollout_lock"))
        self._admission, self._held = admission, held

    def _release_unfinished(self, admission: _RolloutPathLock, held: _RolloutLockHold) -> None:
        if self._depth == 0 and self._held is held:
            self._admission, self._held = None, None
        with _storage_io(self._path, "rollout 文件锁 I/O 失败", "release", artifact="rollout_lock"):
            admission.release(held)

    def _open_handle(self) -> BinaryIO:
        with _storage_io(self._path, "rollout 文件锁 I/O 失败", "open", artifact="rollout_lock"):
            if self._exclusive:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._path.open("a+b" if self._exclusive else "rb")
        try:
            if os.name == "nt":
                # msvcrt 独占锁需要可锁定的首字节；portalocker 为共享锁使用 Win32 LockFileEx。
                handle.seek(0, os.SEEK_END)
                if self._exclusive and handle.tell() == 0:
                    handle.write(b" ")
                    handle.flush()
                handle.seek(0)
            return handle
        except OSError as error:
            with _storage_io(self._path, "rollout 文件锁 I/O 失败", "close", artifact="rollout_lock"):
                handle.close()
            raise RuntimeError(_storage_failure(self._path, "rollout 文件锁获取失败", "initialize", error, artifact="rollout_lock")) from error
        except BaseException:
            with _storage_io(self._path, "rollout 文件锁 I/O 失败", "close", artifact="rollout_lock"):
                handle.close()
            raise

    def _file_lock_delay(self, handle: BinaryIO, deadline: float) -> float | None:
        if time.monotonic() >= deadline:
            raise TimeoutError(
                _storage_failure(self._path, f"rollout 文件锁获取超时 timeout_seconds={self._timeout_seconds:g}", "file_acquisition", artifact="rollout_lock")
            )
        with _storage_io(self._path, "rollout 文件锁 I/O 失败", "file_acquisition", artifact="rollout_lock"):
            mode = portalocker.LOCK_EX if self._exclusive else portalocker.LOCK_SH
            try:
                portalocker.lock(
                    handle.fileno(), mode | portalocker.LOCK_NB
                )
            except portalocker.AlreadyLocked:
                return min(
                    _ROLLOUT_FILE_LOCK_POLL_INTERVAL_SECONDS,
                    max(0, deadline - time.monotonic()),
                )
            except portalocker.LockException as error:
                raise RuntimeError(
                    _storage_failure(self._path, "rollout 文件锁获取失败", "file_acquisition", error, artifact="rollout_lock")
                ) from error
        return None

    def _acquire_handle(self, deadline: float) -> BinaryIO:
        handle = self._open_handle()
        try:
            while (delay := self._file_lock_delay(handle, deadline)) is not None:
                time.sleep(delay)
            return handle
        except BaseException:
            with _storage_io(self._path, "rollout 文件锁 I/O 失败", "close", artifact="rollout_lock"):
                handle.close()
            raise

    async def _acquire_handle_async(self, deadline: float) -> BinaryIO:
        handle = self._open_handle()
        try:
            while (delay := self._file_lock_delay(handle, deadline)) is not None:
                await asyncio.sleep(delay)
            return handle
        except BaseException:
            with _storage_io(self._path, "rollout 文件锁 I/O 失败", "close", artifact="rollout_lock"):
                handle.close()
            raise

    def release(self) -> None:
        admission, held = self._admission, self._held
        if admission is None or held is None:
            return
        self._depth -= 1
        if not self._depth:
            self._admission, self._held = None, None
        with _storage_io(self._path, "rollout 文件锁 I/O 失败", "release", artifact="rollout_lock"):
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

    @classmethod
    def open(
        cls, index_path: Path, *, read_only: bool, frozen_source: bool,
    ) -> sqlite3.Connection:
        """打开唯一 rollout 连接，文件形态与 SQLite 初始化由连接类型持有。"""
        validate_sqlite_path_budget(index_path)
        rollout_root = native_io_path(index_path.parent)
        physical_index = native_io_path(index_path)
        with _storage_io(index_path, "rollout index 路径校验失败", "path_validation", artifact="rollout_index"):
            if rollout_root.is_symlink():
                raise RuntimeError(_storage_failure(rollout_root, "rollout 目录不能是符号链接", "path_validation", artifact="rollout_index"))
            if rollout_root.exists() and not rollout_root.is_dir():
                raise RuntimeError(_storage_failure(rollout_root, "rollout 路径必须是普通目录", "path_validation", artifact="rollout_index"))
            if physical_index.is_symlink():
                raise RuntimeError(_storage_failure(index_path, "rollout index.sqlite 不能是符号链接", "path_validation", artifact="rollout_index"))
            if physical_index.exists() and not physical_index.is_file():
                raise RuntimeError(_storage_failure(index_path, "rollout index.sqlite 必须是普通文件", "path_validation", artifact="rollout_index"))
        # 不能裸 open/read/close 数据库文件：POSIX close 会释放同进程
        # 其它 SQLite 连接的文件锁。格式校验由下方 SQLite schema 查询完成。
        # 在线只读连接仍读已提交 WAL；仅冻结快照使用 immutable。
        parameters = "?mode=ro" + ("&immutable=1" if frozen_source else "") if read_only else ""
        connection = sqlite3.connect(
            sqlite_file_uri(index_path) + parameters,
            uri=True,
            timeout=30,
            check_same_thread=False,
            factory=cls,
        )
        try:
            # 先做只读 schema 探测，避免对损坏的 index.sqlite 直接执行
            # journal_mode=WAL；SQLite 可能因此创建一个看似可用的空数据库。
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            if read_only:
                connection.execute("PRAGMA query_only = ON")
            else:
                connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
        except BaseException:
            connection.close()
            raise
        return connection

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
