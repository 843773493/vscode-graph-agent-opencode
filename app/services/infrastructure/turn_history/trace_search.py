"""原 Trace 页的有界正文查询与单记录定位，不维护正文副本或搜索索引。"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.event import Event
from app.schemas.internal_v2.trace_search import (
    TraceLocationDTO,
    TraceMatchDTO,
    TraceReferenceDTO,
    TraceSearchLimit,
)
from app.services.mapping.trace_event_mapper import TraceEventMapper

from .trace_cursor import TraceCursorGoneError
from .trace_page import (
    TRACE_PAGE_MAX_BYTES,
    TracePageBoundaryError,
    TracePageBudgetExceededError,
    parse_trace_record,
    read_trace_event_page,
)

MAX_SEARCH_BYTES = 8 * 1024 * 1024
MAX_SEARCH_RECORDS = 128


class TraceSearchError(ValueError):
    def __init__(self, code: Literal["invalid_trace_search_cursor", "stale_trace_search_cursor", "stale_trace_reference"],
                 reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason

    def detail(self) -> dict[str, str]:
        return {"owner": "trace_event_store", "code": self.code, "reason": self.reason, "message": str(self)}


class TraceSearchPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_revision: str
    before_offset: int = Field(ge=0)
    offset: int = Field(default=0, ge=0)
    content_version: str | None = None


@dataclass
class TraceSearchBudget:
    check: Callable[[], None]
    deadline: float = field(default_factory=lambda: monotonic() + 1.0)
    scanned_records: int = 0
    scanned_bytes: int = 0

    def limited_reason(self, *, include_bytes: bool = True) -> TraceSearchLimit | None:
        self.check()
        if monotonic() >= self.deadline:
            return "deadline"
        if self.scanned_records >= MAX_SEARCH_RECORDS:
            return "scan_limit"
        if include_bytes and self.scanned_bytes >= MAX_SEARCH_BYTES:
            return "byte_limit"
        return None


@dataclass
class TraceSearchPage:
    items: list[TraceMatchDTO]
    position: TraceSearchPosition | None = None
    complete: bool = True
    limited_reason: TraceSearchLimit | None = None


def _file_version(file: Path) -> tuple[str, int] | None:
    try:
        stat = file.stat()
    except FileNotFoundError:
        return None
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}", stat.st_size


def _text(event: Event) -> str:
    return json.dumps(event.model_dump(mode="json"), ensure_ascii=False, indent=2)


def search_trace_events(
    session_id: str, file: Path, query: str, *, limit: int, position: TraceSearchPosition | None,
    budget: TraceSearchBudget, event_threads: Callable[[list[Event]], dict[str, str]],
) -> TraceSearchPage:
    observed = _file_version(file)
    if observed is None:
        if position is not None:
            raise TraceSearchError("stale_trace_search_cursor", "file_deleted", "Trace 文件已删除，请重新查询")
        return TraceSearchPage([])
    revision, size = observed
    if position is not None and position.file_revision != revision:
        raise TraceSearchError("stale_trace_search_cursor", "file_changed", "Trace 已追加或改写，请重新查询")
    before = position.before_offset if position else size
    offset = position.offset if position else 0
    if before > size or before == 0 and offset:
        raise TraceSearchError("invalid_trace_search_cursor", "position_out_of_bounds", "Trace cursor 位置越界")
    items: list[TraceMatchDTO] = []
    while before:
        next_position = TraceSearchPosition(file_revision=revision, before_offset=before, offset=offset,
                                            content_version=position.content_version if position and offset else None)
        reason = budget.limited_reason()
        if reason or len(items) >= limit:
            return TraceSearchPage(items, next_position, False, reason or "result_limit")
        # 固定窗口，续页始终从完整事件边界读取；不因本页剩余预算缩小窗口而误判单事件超限。
        if MAX_SEARCH_BYTES - budget.scanned_bytes < TRACE_PAGE_MAX_BYTES:
            return TraceSearchPage(items, next_position, False, "byte_limit")
        try:
            page = read_trace_event_page(session_id=session_id, file=file, cursor=None,
                before_offset=before, limit=MAX_SEARCH_RECORDS - budget.scanned_records)
        except TracePageBudgetExceededError:
            budget.scanned_bytes += min(before, TRACE_PAGE_MAX_BYTES)
            return TraceSearchPage(items, complete=False, limited_reason="item_size_limit")
        except (TraceCursorGoneError, TracePageBoundaryError) as error:
            raise TraceSearchError("invalid_trace_search_cursor", "position_out_of_bounds", "Trace cursor 位置越界") from error
        budget.scanned_bytes += page.bytes_read
        if not page.records:
            break
        texts = [(record, _text(record.event)) for record in reversed(page.records)]
        candidates = [record.event for record, text in texts if query in text]
        owners: dict[str, str] = {}
        for record, text in texts:
            reason = budget.limited_reason(include_bytes=False)
            if reason:
                return TraceSearchPage(items, TraceSearchPosition(file_revision=revision, before_offset=record.end,
                    offset=offset, content_version=position.content_version if position and offset else None), False, reason)
            budget.scanned_records += 1
            if offset > len(text):
                raise TraceSearchError("invalid_trace_search_cursor", "position_out_of_bounds", "Trace cursor 正文位置越界")
            version = hashlib.sha256(record.line).hexdigest() if offset or query in text else ""
            if offset and (position is None or position.content_version != version):
                raise TraceSearchError("stale_trace_search_cursor", "record_changed", "Trace 当前记录已变化，请重新查询")
            while (start := text.find(query, offset)) >= 0:
                # 归属解析可能耗尽预算，先交付当前命中，再按预算返回续页。
                if not owners:
                    owners = event_threads(candidates)
                end = start + len(query)
                preview_start = max(0, start - 80)
                items.append(TraceMatchDTO(reference=TraceReferenceDTO(
                    session_id=session_id, thread_id=owners[record.event.event_id], event_id=record.event.event_id,
                    line_start=record.start, line_end=record.end, content_version=version,
                    start_codepoint=start, end_codepoint=end),
                    event_type=record.event.type, timestamp=record.event.timestamp,
                    preview=text[preview_start:min(len(text), end + 80)], preview_start_codepoint=preview_start))
                offset = end
                reason = budget.limited_reason(include_bytes=False)
                if len(items) >= limit or reason:
                    if text.find(query, offset) >= 0:
                        return TraceSearchPage(items, TraceSearchPosition(file_revision=revision,
                            before_offset=record.end, offset=offset, content_version=version), False, reason or "result_limit")
                    before, offset = record.start, 0
                    return TraceSearchPage(items, TraceSearchPosition(file_revision=revision, before_offset=before)
                        if before else None, not before, (reason or "result_limit") if before else None)
            before, offset = record.start, 0
        # 按原页的窗口边界推进，空白行不形成另一套扫描链。
        if not page.has_more:
            before = 0
    return TraceSearchPage(items)


def locate_trace_event(
    file: Path, reference: TraceReferenceDTO, *, event_threads: Callable[[list[Event]], dict[str, str]],
) -> TraceLocationDTO:
    length = reference.line_end - reference.line_start
    if length > TRACE_PAGE_MAX_BYTES:
        raise TraceSearchError("stale_trace_reference", "record_size_limit", "命中的 Trace 记录超过读取上限")
    try:
        with file.open("rb") as stream:
            stream.seek(reference.line_start)
            raw = stream.read(length)
    except FileNotFoundError as error:
        raise TraceSearchError("stale_trace_reference", "file_deleted", "命中的 Trace 文件已删除") from error
    if hashlib.sha256(raw).hexdigest() != reference.content_version:
        raise TraceSearchError("stale_trace_reference", "record_changed", "命中的 Trace 记录已变化或裁剪")
    record = parse_trace_record(reference.session_id, reference.line_start, reference.line_end, raw)
    thread = event_threads([record.event])[record.event.event_id]
    if (record.event.event_id, thread) != (reference.event_id, reference.thread_id):
        raise TraceSearchError("stale_trace_reference", "owner_mismatch", "命中的 Trace 事件或 Thread 归属不符")
    text = _text(record.event)
    if reference.end_codepoint > len(text):
        raise TraceSearchError("stale_trace_reference", "position_out_of_bounds", "Trace 命中位置已失效")
    dto = TraceEventMapper().map_one(record.event.model_dump(mode="json"), reference.session_id, thread_id=thread)
    if dto is None:
        raise ValueError(f"Trace 事件没有公开详情映射: {record.event.type}")
    return TraceLocationDTO(reference=reference, record=dto, text=text)
