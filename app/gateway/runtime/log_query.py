"""受管诊断文件的有界行查询；不保存正文副本，不参与写入和轮转。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, time
from typing import BinaryIO, Literal

from app.runtime.log_file_identity import (
    runtime_log_file_identity,
    runtime_log_path_identity,
)

LogQueryLimit = Literal["result_limit", "scan_limit", "byte_limit", "deadline", "item_size_limit"]
MAX_LOG_QUERY_LINES = 512
MAX_LOG_QUERY_BYTES = 8 * 1024 * 1024
MAX_LOG_QUERY_LINE_BYTES = 256 * 1024


class RuntimeLogQueryError(ValueError):
    def __init__(self, code: str, reason: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class LogFileObservation:
    device: str
    inode: str
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result, identity: tuple[str, str]) -> LogFileObservation:
        return cls(*identity, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    @property
    def identity(self) -> tuple[str, str]:
        return self.device, self.inode


@dataclass(frozen=True, slots=True)
class LogRecordReference:
    device: str
    inode: str
    offset: int
    length: int
    content_version: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class LogLineMatch:
    reference: LogRecordReference
    preview: str
    preview_start: int


@dataclass(slots=True)
class LogQueryBudget:
    deadline: float
    lines: int = 0
    bytes_read: int = 0

    @classmethod
    def create(cls, deadline_at: float | None = None) -> LogQueryBudget:
        return cls(monotonic() + min(1.0, max(0.0, deadline_at - time()) if deadline_at is not None else 1.0))

    @property
    def limited_reason(self) -> LogQueryLimit | None:
        if monotonic() >= self.deadline:
            return "deadline"
        if self.lines >= MAX_LOG_QUERY_LINES:
            return "scan_limit"
        if self.bytes_read >= MAX_LOG_QUERY_BYTES:
            return "byte_limit"
        return None


@dataclass(frozen=True, slots=True)
class LogFileQueryPage:
    observation: LogFileObservation
    items: list[LogLineMatch]
    offset: int
    match_offset: int
    limited_reason: LogQueryLimit | None = None


def _stale_cursor(reason: str, message: str) -> RuntimeLogQueryError:
    return RuntimeLogQueryError("stale_runtime_log_cursor", reason, message)


@contextmanager
def open_runtime_log(path: Path) -> Iterator[tuple[BinaryIO, LogFileObservation]]:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("诊断日志路径必须是普通文件，不能是符号链接")
    with path.open("rb") as stream:
        identity = runtime_log_file_identity(stream.fileno())
        if runtime_log_path_identity(path) != identity:
            raise _stale_cursor("file_changed", "诊断日志在打开时变化，请重新查询")
        opened = LogFileObservation.from_stat(
            os.fstat(stream.fileno()), identity,
        )
        yield stream, opened


def _check_observation(path: Path, stream: BinaryIO, observation: LogFileObservation) -> None:
    try:
        current = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise _stale_cursor("file_deleted", "诊断日志已删除，请重新查询") from error
    if not stat.S_ISREG(current.st_mode) or runtime_log_path_identity(path) != observation.identity or (
        LogFileObservation.from_stat(os.fstat(stream.fileno()),
            runtime_log_file_identity(stream.fileno())) != observation
    ):
        raise _stale_cursor("file_changed", "诊断日志已追加、轮转或改写，请重新查询")


def query_runtime_log(
    path: Path, query: str, *, limit: int, budget: LogQueryBudget,
    offset: int = 0, match_offset: int = 0, observation: LogFileObservation | None = None,
) -> LogFileQueryPage:
    """按原始物理行正序匹配，位置是去除行尾后的 Unicode 码点区间。"""
    if not query or "\n" in query or "\r" in query:
        raise ValueError("运行日志查询需要非空的单行字面量")
    if limit < 1:
        raise ValueError("运行日志查询的命中配额必须大于零")
    items: list[LogLineMatch] = []
    with open_runtime_log(path) as (stream, current):
        if observation is not None and current != observation:
            raise _stale_cursor("file_changed", "续查的诊断日志已追加、轮转或改写")
        if offset < 0 or offset > current.size or match_offset < 0 or (offset == current.size and match_offset):
            raise RuntimeLogQueryError("invalid_runtime_log_cursor", "position_out_of_bounds", "日志续点越界")
        if offset:
            stream.seek(offset - 1)
            if offset != current.size and stream.read(1) != b"\n":
                raise RuntimeLogQueryError("invalid_runtime_log_cursor", "position_out_of_bounds", "日志续点不在原行边界")
        reason: LogQueryLimit | None = None
        while offset < current.size:
            reason = budget.limited_reason
            if reason is not None:
                break
            stream.seek(offset)
            allowance = min(MAX_LOG_QUERY_LINE_BYTES + 1, MAX_LOG_QUERY_BYTES - budget.bytes_read, current.size - offset)
            raw = stream.readline(allowance)
            budget.bytes_read += len(raw)
            if len(raw) > MAX_LOG_QUERY_LINE_BYTES:
                reason = "item_size_limit"
                break
            if not raw.endswith(b"\n") and offset + len(raw) < current.size:
                reason = "byte_limit"
                break
            text = raw.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8", errors="replace")
            if match_offset > len(text):
                raise RuntimeLogQueryError("invalid_runtime_log_cursor", "position_out_of_bounds", "日志行内续点越界")
            budget.lines += 1
            position = text.find(query, match_offset)
            version = hashlib.sha256(raw).hexdigest() if position >= 0 else ""
            while position >= 0:
                end = position + len(query)
                preview_start = max(0, position - 160)
                items.append(LogLineMatch(
                    LogRecordReference(current.device, current.inode, offset, len(raw), version, position, end),
                    text[preview_start:min(len(text), end + 240)], preview_start,
                ))
                match_offset = end
                position = text.find(query, match_offset)
                if len(items) == limit:
                    if position < 0:
                        offset += len(raw)
                        match_offset = 0
                    reason = "result_limit" if offset < current.size else None
                    break
            else:
                offset += len(raw)
                match_offset = 0
                continue
            break
        _check_observation(path, stream, current)
    return LogFileQueryPage(current, items, offset, match_offset, reason)


def read_runtime_log_record(path: Path, reference: LogRecordReference) -> str:
    """只读目标区间及边界；完整行摘要允许追加后的原记录继续定位。"""
    def stale(reason: str, message: str) -> RuntimeLogQueryError:
        return RuntimeLogQueryError("stale_runtime_log_reference", reason, message)

    if reference.offset < 0 or not 1 <= reference.length <= MAX_LOG_QUERY_LINE_BYTES:
        raise stale("position_out_of_bounds", "运行日志记录区间无效")
    try:
        with open_runtime_log(path) as (stream, observation):
            if observation.identity != (reference.device, reference.inode):
                raise stale("file_replaced", "原运行日志已轮转或替换")
            if reference.offset + reference.length > observation.size:
                raise stale("record_changed", "原运行日志记录已被裁剪")
            if reference.offset:
                stream.seek(reference.offset - 1)
                if stream.read(1) != b"\n":
                    raise stale("record_changed", "原运行日志行边界已变化")
            stream.seek(reference.offset)
            raw = stream.read(reference.length)
            if hashlib.sha256(raw).hexdigest() != reference.content_version or b"\n" in raw[:-1] or (
                not raw.endswith(b"\n") and stream.read(1)
            ):
                raise stale("record_changed", "原运行日志记录已改写")
            text = raw.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8", errors="replace")
            if not 0 <= reference.start < reference.end <= len(text):
                raise stale("position_out_of_bounds", "运行日志命中位置已失效")
            current = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or runtime_log_path_identity(path) != observation.identity:
                raise stale("file_replaced", "原运行日志已轮转或替换")
            return text
    except FileNotFoundError as error:
        raise stale("file_deleted", "原运行日志已删除") from error
    except RuntimeLogQueryError as error:
        if error.code == "stale_runtime_log_cursor":
            raise stale(error.reason, str(error)) from error
        raise
