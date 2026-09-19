"""rollout storage 的锁、快照与只读值对象。

这些类型不承载业务规则；它们只定义跨 JSONL/SQLite 读写的生命周期与快照
边界，避免 RolloutStorage facade 继续膨胀。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, Self

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)
from app.services.infrastructure.rollout_context.storage.guards import (
    pop_jsonl_guard,
    truncate_uncommitted_tail,
)


class MessageCodec(Protocol):
    """checkpoint 组装层注入的消息适配端口。"""

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
        turn_id: str,
        timestamp: str,
        model_call_id: str | None = None,
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
    """从 active view 解析出的完整 Turn 用户锚点。"""

    turn_id: str
    view_id: str
    checkpoint_id: str
    branch_id: str
    logical_turn_ordinal: int
    first_message_sequence: int
    user_message_sequence: int
    last_message_sequence: int
    final_message_sequence: int | None
    anchor_mode: str
    cutoff_message_sequence: int


class _RolloutFileLock:
    """为 JSONL 与 SQLite 的跨文件读取/写入提供进程间文件锁。"""

    def __init__(
        self,
        path: Path,
        *,
        exclusive: bool,
        timeout_seconds: float = _ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("rollout 文件锁 timeout_seconds 必须大于 0")
        self._path = path
        self._exclusive = exclusive
        self._timeout_seconds = timeout_seconds
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            if os.name == "nt":
                # TODO: Windows CI 覆盖共享读锁；Windows 当前使用独占锁保证跨文件一致性。
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b" ")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                mode = fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH
                deadline = time.monotonic() + self._timeout_seconds
                while True:
                    try:
                        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                    except BlockingIOError as error:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                "rollout 文件锁获取超时: "
                                f"path={self._path} timeout_seconds={self._timeout_seconds:g}"
                            ) from error
                        time.sleep(
                            min(_ROLLOUT_FILE_LOCK_POLL_INTERVAL_SECONDS, remaining)
                        )
                    else:
                        break
        except TimeoutError:
            handle.close()
            raise
        except (BlockingIOError, OSError) as error:
            handle.close()
            raise RuntimeError(f"rollout 文件锁获取失败: {self._path}") from error
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None


class _RolloutOperationLock:
    """同一进程可重入、跨进程独占的 rollout 写入锁。"""

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = _ROLLOUT_FILE_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._thread_lock = threading.RLock()
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._depth = 0
        self._file_lock: _RolloutFileLock | None = None

    def __enter__(self) -> Self:
        acquired = self._thread_lock.acquire(timeout=self._timeout_seconds)
        if not acquired:
            raise TimeoutError(
                "rollout 进程内写锁获取超时: "
                f"path={self._path} timeout_seconds={self._timeout_seconds:g}"
            )
        if self._depth == 0:
            file_lock = _RolloutFileLock(self._path, exclusive=True)
            try:
                file_lock.acquire()
            except Exception:
                self._thread_lock.release()
                raise
            self._file_lock = file_lock
        self._depth += 1
        return self

    def __exit__(self, *_: object) -> None:
        self._depth -= 1
        try:
            if self._depth == 0:
                file_lock = self._file_lock
                self._file_lock = None
                if file_lock is not None:
                    file_lock.release()
        finally:
            self._thread_lock.release()


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
