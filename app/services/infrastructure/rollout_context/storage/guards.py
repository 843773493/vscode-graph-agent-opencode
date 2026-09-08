"""跨 JSONL/SQLite 事务的文件尾保护。"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class JsonlTransactionGuard:
    path: Path
    original_size: int
    commit_attempted: bool = False


_GUARDS: dict[int, JsonlTransactionGuard] = {}


def register_jsonl_guard(
    connection: sqlite3.Connection,
    path: Path,
    *,
    original_size: int,
) -> None:
    """登记外层 SQLite 事务负责的 JSONL 原始长度。"""
    existing = _GUARDS.get(id(connection))
    if existing is None:
        _GUARDS[id(connection)] = JsonlTransactionGuard(
            path=path,
            original_size=original_size,
        )
        return
    if existing.path != path:
        raise RuntimeError("同一 SQLite 事务不能跨越多个 rollout JSONL")
    existing.original_size = min(existing.original_size, original_size)


def mark_jsonl_commit_attempted(connection: sqlite3.Connection) -> None:
    guard = _GUARDS.get(id(connection))
    if guard is not None:
        guard.commit_attempted = True


def pop_jsonl_guard(connection: sqlite3.Connection) -> JsonlTransactionGuard | None:
    return _GUARDS.pop(id(connection), None)


def truncate_uncommitted_tail(guard: JsonlTransactionGuard) -> None:
    """回滚未提交的 JSONL 尾部；调用方必须已持有 rollout 文件锁。"""
    if not guard.path.exists():
        return
    if guard.path.stat().st_size <= guard.original_size:
        return
    with guard.path.open("r+b") as stream:
        stream.truncate(guard.original_size)
        stream.flush()
        os.fsync(stream.fileno())


__all__ = [
    "JsonlTransactionGuard",
    "mark_jsonl_commit_attempted",
    "pop_jsonl_guard",
    "register_jsonl_guard",
    "truncate_uncommitted_tail",
]
